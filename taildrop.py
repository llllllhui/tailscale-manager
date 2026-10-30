"""Taildrop 文件收发：使用已有守护进程，独立于图形界面。"""

import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote


class TaildropError(Exception):
    pass


def safe_name(name):
    if not isinstance(name, str) or not name or name in (".", "..") or any(
            character in name for character in ("/", "\\", "\0")):
        raise TaildropError("收件文件名无效，文件仍保留在 Tailscale 收件箱")
    return name


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, stopped):
        super().__init__("local-tailscaled.sock", timeout=10)
        self.path = str(path)
        self.stopped = stopped

    def connect(self):
        if self.stopped.is_set():
            raise TaildropError("文件传输已停止")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)
        if self.stopped.is_set():
            self.close()
            raise TaildropError("文件传输已停止")


class LocalAPI:
    def __init__(self, socket_path=None):
        self.socket_path = socket_path
        self.connections = set()
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def connection(self):
        path = self.socket_path
        if path is None:
            path = next((p for p in (
                "/var/run/tailscale/tailscaled.sock",
                "/home/linuxbrew/.linuxbrew/var/run/tailscale/tailscaled.sock",
            ) if Path(p).is_socket()), None)
        if path is None:
            raise TaildropError("找不到 Tailscale 本地接口，请先启动 Tailscale")
        connection = UnixConnection(path, self.stopped)
        with self.lock:
            if self.stopped.is_set():
                raise TaildropError("文件传输已停止")
            self.connections.add(connection)
        return connection

    def release(self, connection):
        connection.close()
        with self.lock:
            self.connections.discard(connection)

    @staticmethod
    def checked(response, expected=200):
        if response.status != expected:
            text = response.read(8192).decode("utf-8", errors="replace").strip()
            if response.status in (401, 403):
                raise TaildropError("没有文件传输权限，请确认本机已授权为 Tailscale operator。" + text)
            raise TaildropError(text or "Tailscale 文件接口返回错误 %s" % response.status)

    def json(self, path):
        connection = self.connection()
        try:
            connection.request("GET", "/localapi/v0/" + path)
            response = connection.getresponse()
            self.checked(response)
            value = json.loads(response.read())
            if value is not None and not isinstance(value, list):
                raise TaildropError("Tailscale 文件接口的数据格式不受支持")
            if value and not all(isinstance(item, dict) for item in value):
                raise TaildropError("Tailscale 文件接口的数据格式不受支持")
            return value or []
        finally:
            self.release(connection)

    def delete(self, name):
        connection = self.connection()
        try:
            connection.request("DELETE", "/localapi/v0/files/" + quote(safe_name(name), safe=""))
            response = connection.getresponse()
            self.checked(response, 204)
            response.read()
        finally:
            self.release(connection)

    def receive(self, name, directory, stopped):
        name = safe_name(name)
        connection = self.connection()
        target = None
        descriptor = None
        try:
            connection.request("GET", "/localapi/v0/files/" + quote(name, safe=""))
            response = connection.getresponse()
            self.checked(response)
            length = response.getheader("Content-Length")
            if length is None or not length.isdigit():
                raise TaildropError("收件大小无效，文件仍保留在收件箱")
            length = int(length)
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            for number in range(10000):
                candidate = name if number == 0 else "%s (%d)%s" % (
                    Path(name).stem, number, Path(name).suffix)
                candidate = directory / candidate
                try:
                    descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    target = candidate
                    break
                except FileExistsError:
                    target = None
            if descriptor is None:
                raise TaildropError("同名文件过多，请选择其他接收文件夹")
            written = 0
            with os.fdopen(descriptor, "wb") as output:
                descriptor = None
                while True:
                    if stopped.is_set():
                        raise TaildropError("接收已停止，原文件仍在收件箱")
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    written += len(chunk)
                if written != length:
                    raise TaildropError("文件接收中断，原文件仍在收件箱，可重新收取")
                output.flush()
                os.fsync(output.fileno())
            return target, written
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            if target is not None:
                target.unlink(missing_ok=True)
            raise
        finally:
            self.release(connection)

    def close(self):
        with self.lock:
            self.stopped.set()
            connections = list(self.connections)
        for connection in connections:
            if connection.sock:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()


class TaildropManager:
    def __init__(self, binary, directory, auto_receive=True, on_received=None, client=None):
        self.binary = binary
        self.directory = Path(directory).expanduser().resolve()
        self.auto_receive = bool(auto_receive)
        self.on_received = on_received or (lambda _items: None)
        self.client = client or LocalAPI()
        self.lock = threading.RLock()
        self.poll_lock = threading.Lock()
        self.stopped = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="taildrop-send")
        self.targets = []
        self.pending = []
        self.transfers = []
        self.processes = {}
        self.cancellations = {}
        self.acknowledgements = {}
        self.receive_failures = {}
        self.error = ""
        self.receiving = False

    def snapshot(self):
        with self.lock:
            active = [item for item in self.transfers if item["status"] in ("queued", "sending")]
            completed = [item for item in self.transfers if item["status"] not in ("queued", "sending")]
            visible = {item["id"] for item in active + completed[-50:]}
            return {"directory": str(self.directory), "autoReceive": self.auto_receive,
                    "targets": [dict(item) for item in self.targets],
                    "pending": [dict(item) for item in self.pending],
                    "transfers": [dict(item) for item in self.transfers if item["id"] in visible][::-1],
                    "error": self.error, "receiving": self.receiving}

    def configure(self, directory=None, auto_receive=None):
        with self.lock:
            if directory is not None:
                directory = Path(directory).expanduser().resolve()
                if not directory.is_dir():
                    raise TaildropError("请选择有效的接收文件夹")
                self.directory = directory
            if auto_receive is not None:
                self.auto_receive = bool(auto_receive)

    def poll(self, receive=False):
        if self.stopped.is_set() or not self.poll_lock.acquire(blocking=False):
            return
        try:
            errors = []
            try:
                targets = []
                for entry in self.client.json("file-targets"):
                    node = entry.get("Node") or {}
                    if not isinstance(node, dict):
                        raise TaildropError("Tailscale 设备信息格式不受支持")
                    addresses = node.get("Addresses") or []
                    for address in addresses:
                        ip = ipaddress.ip_interface(address).ip
                        if ip.version == 4:
                            targets.append({"ip": str(ip), "name": node.get("ComputedName")
                                            or node.get("Name", "").rstrip(".") or str(ip),
                                            "online": node.get("Online") is not False})
                            break
                with self.lock:
                    self.targets = targets
            except (OSError, ValueError, TaildropError, http.client.HTTPException) as exc:
                errors.append(str(exc))
            try:
                waiting = self.client.json("files/")
                pending = []
                for item in waiting:
                    size = item.get("Size", 0)
                    if not isinstance(size, int) or size < 0:
                        raise TaildropError("收件大小无效，原文件仍保留在收件箱")
                    pending.append({"name": safe_name(item.get("Name")), "size": size})
                with self.lock:
                    self.pending = pending
                    enabled = self.auto_receive or receive
                if enabled:
                    self.receive_pending()
            except (OSError, ValueError, TaildropError, http.client.HTTPException) as exc:
                errors.append(str(exc))
            with self.lock:
                self.error = "；".join(errors)
        finally:
            self.poll_lock.release()

    def receive_pending(self):
        received = []
        with self.lock:
            self.receiving = True
            pending = list(self.pending)
            directory = self.directory
        try:
            for item in pending:
                if self.stopped.is_set():
                    break
                name = item["name"]
                if name not in self.acknowledgements:
                    try:
                        target, size = self.client.receive(name, directory, self.stopped)
                    except (OSError, ValueError, TaildropError, http.client.HTTPException) as exc:
                        with self.lock:
                            record = self.receive_failures.get(name)
                            if record is None:
                                record = self.record("receive", name, item["size"], "本机", "failed")
                                self.receive_failures[name] = record
                            record["error"] = str(exc)
                        raise
                    with self.lock:
                        record = self.receive_failures.pop(name, None) or self.record(
                            "receive", target.name, size, "本机", "done")
                        record.update(name=target.name, size=size, status="done", error="", path=str(target))
                    self.acknowledgements[name] = record
                    received.append(dict(record))
                self.client.delete(name)
                self.acknowledgements.pop(name, None)
                with self.lock:
                    self.pending = [entry for entry in self.pending if entry["name"] != name]
        finally:
            with self.lock:
                self.receiving = False
            if received:
                self.on_received(received)

    def record(self, direction, name, size, peer, status="queued"):
        record = {"id": uuid.uuid4().hex, "direction": direction, "name": name,
                  "size": size, "peer": peer, "status": status, "time": int(time.time()), "error": ""}
        with self.lock:
            self.transfers.append(record)
            # 活跃发件不能被清理，以便始终可以取消。
            completed = [item for item in self.transfers if item["status"] not in ("queued", "sending")]
            for item in completed[:-50]:
                self.transfers.remove(item)
        return record

    def send(self, files, ip):
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError as exc:
            raise TaildropError("无效的接收设备") from exc
        with self.lock:
            target = next((target for target in self.targets if target["ip"] == ip), None)
        if not target:
            raise TaildropError("该设备不能接收文件；请确认属于同一账号，且已启用 Taildrop")
        if not target["online"]:
            raise TaildropError("接收设备当前离线，请上线后重试")
        if not self.binary or self.stopped.is_set():
            raise TaildropError("Tailscale 文件传输尚未就绪")
        paths = []
        for value in files:
            path = Path(value).expanduser().resolve(strict=True)
            if not stat.S_ISREG(path.stat().st_mode):
                raise TaildropError("请选择普通文件；文件夹请先打包为压缩文件")
            paths.append(path)
        if not paths:
            raise TaildropError("请至少选择一个文件")
        if len(paths) > 100:
            raise TaildropError("一次最多选择 100 个文件")
        for path in paths:
            record = self.record("send", path.name, path.stat().st_size, target["name"])
            cancelled = threading.Event()
            with self.lock:
                self.cancellations[record["id"]] = cancelled
            self.executor.submit(self.send_one, record, path, ip, cancelled)
        return {"ok": True, "message": "已加入发送队列，共 %d 个文件" % len(paths)}

    def send_one(self, record, path, ip, cancelled):
        process = None
        try:
            if cancelled.is_set() or self.stopped.is_set():
                return
            with self.lock:
                record["status"] = "sending"
            process = subprocess.Popen(
                [self.binary, "file", "cp", "--update-interval=0", str(path), ip + ":"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            with self.lock:
                self.processes[record["id"]] = process
            while True:
                if cancelled.is_set() or self.stopped.is_set():
                    process.terminate()
                    try:
                        process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    return
                try:
                    output, _ = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            with self.lock:
                record["status"] = "done" if process.returncode == 0 else "failed"
                record["error"] = "" if process.returncode == 0 else (output or "发送失败").strip()[-4096:]
        except (OSError, ValueError) as exc:
            with self.lock:
                record.update(status="failed", error=str(exc))
        finally:
            with self.lock:
                if (cancelled.is_set() or self.stopped.is_set()) and record["status"] not in ("done", "failed"):
                    record["status"] = "cancelled"
                self.processes.pop(record["id"], None)
                self.cancellations.pop(record["id"], None)

    def cancel(self, identifier):
        if not isinstance(identifier, str):
            raise TaildropError("无效的传输记录")
        with self.lock:
            cancelled = self.cancellations.get(identifier)
            if cancelled:
                cancelled.set()
            record = next((item for item in self.transfers if item["id"] == identifier), None)
            if record and record["status"] == "queued":
                record["status"] = "cancelled"
        return {"ok": True}

    def close(self):
        self.stopped.set()
        with self.lock:
            for cancelled in self.cancellations.values():
                cancelled.set()
        self.client.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
