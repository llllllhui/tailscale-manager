#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tailscale Manager 本地服务：只依赖标准库，前端由浏览器渲染。"""

import json
import ipaddress
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(APP_DIR, "ui.html")


def find_ts():
    """优先返回能实际跑起来的 tailscale 可执行文件。

    注意：/usr/local/bin/tailscale 这类软链会让 Homebrew 的二进制找不到
    自带的 libc，从而报共享库错误，所以这里显式优先 brew 的真实路径。
    """
    for cand in (
        "/home/linuxbrew/.linuxbrew/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
        "/usr/local/bin/tailscale",
        "/usr/bin/tailscale",
    ):
        if os.path.exists(cand) and _works(cand):
            return cand
    p = shutil.which("tailscale")
    if p and _works(p):
        return p
    for cand in ("/home/linuxbrew/.linuxbrew/bin/tailscale", "/usr/bin/tailscale"):
        if os.path.exists(cand):
            return cand
    return None


def _works(path):
    try:
        r = subprocess.run([path, "version"], capture_output=True, text=True, timeout=6)
        return r.returncode == 0
    except Exception:
        return False


TAILSCALE = find_ts()


def run(args, timeout=20):
    try:
        p = subprocess.run([TAILSCALE] + args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "命令超时"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def build_state():
    if not TAILSCALE:
        return {"ok": False, "error": "未找到可用的 tailscale 命令"}

    rc, out, err = run(["status", "--json"])
    if rc != 0 or not out.strip():
        return {"ok": False, "error": (err or "tailscaled 未运行").strip()}

    try:
        s = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": "状态解析失败：%s" % exc}

    prefs = {}
    rc2, out2, _ = run(["debug", "prefs"], timeout=10)
    if rc2 == 0 and out2.strip():
        try:
            prefs = json.loads(out2)
        except json.JSONDecodeError:
            prefs = {}

    ips = s.get("TailscaleIPs") or []
    ipv4 = next((x for x in ips if ":" not in x), "")
    ipv6 = next((x for x in ips if ":" in x), "")
    me = s.get("Self") or {}

    users = {}
    for uid, u in (s.get("User") or {}).items():
        users[str(uid)] = u.get("LoginName") or ""

    peers = []
    selected_exit = prefs.get("ExitNodeIP") or ""
    for pid, p in (s.get("Peer") or {}).items():
        pips = p.get("TailscaleIPs") or []
        pip4 = next((x for x in pips if ":" not in x), "")
        if p.get("ExitNode"):
            selected_exit = pip4
        peers.append({
            "id": pid,
            "name": p.get("HostName") or pip4,
            "ip": pip4,
            "ipv6": next((x for x in pips if ":" in x), ""),
            "os": p.get("OS") or "",
            "online": bool(p.get("Online")),
            "direct": bool(p.get("CurAddr")),
            "relay": p.get("Relay") or "",
            "exitOption": bool(p.get("ExitNodeOption")),
            "user": users.get(str(p.get("UserID")), ""),
            "lastSeen": p.get("LastSeen") or "",
        })
    peers.sort(key=lambda x: (not x["online"], x["name"].lower()))

    return {
        "ok": True,
        "backendState": s.get("BackendState") or "NoState",
        "version": s.get("Version") or "",
        "tailnet": (s.get("MagicDNSSuffix") or "").rstrip("."),
        "health": s.get("Health") or [],
        "authURL": s.get("AuthURL") or "",
        "self": {
            "hostname": me.get("HostName") or "",
            "dnsName": (me.get("DNSName") or "").rstrip("."),
            "ipv4": ipv4,
            "ipv6": ipv6,
            "os": me.get("OS") or "",
            "online": bool(me.get("Online")),
            "keyExpiry": me.get("KeyExpiry") or "",
        },
        "exitNodeIP": selected_exit,
        "allowLanAccess": bool(prefs.get("ExitNodeAllowLANAccess")),
        "shieldsUp": bool(prefs.get("ShieldsUp")),
        "peers": peers,
        "tsPath": TAILSCALE,
        "serverTime": int(__import__("time").time()),
    }


def act(args, ok_msg):
    rc, out, err = run(args, timeout=25)
    if rc == 0:
        return {"ok": True, "message": ok_msg}
    detail = (err or out).strip()
    low = detail.lower()
    need_op = any(word in low for word in ("access denied", "permission", "operator"))
    return {
        "ok": False,
        "error": detail or "操作失败",
        "needOperator": need_op,
        "hint": "需要授权，请执行：sudo %s set --operator=$USER" % TAILSCALE if need_op else "",
    }


def action(path, data=None):
    """桌面窗口、托盘和开发用 HTTP 服务共用的操作入口。"""
    data = {} if data is None else data
    if not isinstance(data, dict):
        return {"ok": False, "error": "无效的请求"}
    if path == "/api/state":
        return build_state()
    if not TAILSCALE:
        return {"ok": False, "error": "未找到 tailscale，请先安装并启动 Tailscale"}
    if path in ("/api/up", "/api/login"):
        result = act(["up", "--timeout=15s"], "已连接")
        if not result["ok"] and not result.get("needOperator"):
            state = build_state()
            url = state.get("authURL") or ""
            if not url:
                match = re.search(r"https://[^\s]+", result.get("error", ""))
                url = match.group(0) if match else ""
            if url:
                return {"ok": True, "url": url, "message": "请在浏览器中完成登录"}
        return result
    if path == "/api/down":
        return act(["down"], "已断开连接")
    if path in ("/api/exit-node", "/api/ping"):
        ip = data.get("ip", "")
        if not isinstance(ip, str):
            return {"ok": False, "error": "无效的 IP 地址"}
        ip = ip.strip()
        if ip or path == "/api/ping":
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                return {"ok": False, "error": "无效的 IP 地址"}
        if path == "/api/exit-node":
            return act(["set", "--exit-node=%s" % ip],
                       "已切换出口节点" if ip else "已取消出口节点")
        rc, out, err = run(["ping", "-c", "1", "--timeout", "3s", ip], timeout=12)
        detail = (out or err or "").strip()
        ms = detail.rsplit(" in ", 1)[1].strip() if " in " in detail else ""
        return {"ok": rc == 0, "ms": ms, "detail": detail}
    return {"ok": False, "error": "未知操作"}


class Handler(SimpleHTTPRequestHandler):
    server_version = "TailscaleManager/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(UI_FILE, "rb") as fh:
                    body = fh.read().decode("utf-8")
            except OSError:
                self.send_error(500, "ui.html missing")
                return
            # 把首屏状态直接注入，避免打开时闪一下「读取中」
            try:
                boot = json.dumps(build_state(), ensure_ascii=False).replace("<", "\\u003c")
            except Exception:
                boot = "null"
            body = body.replace("/*__BOOT__*/null", boot).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            self._json(build_state())
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._body()

        if path in ("/api/up", "/api/down", "/api/login", "/api/exit-node", "/api/ping"):
            self._json(action(path, data))
        else:
            self.send_error(404)


def pick_port(preferred=8770):
    def free(p):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sk.bind(("127.0.0.1", p))
                return True
            except OSError:
                return False

    if free(preferred):
        return preferred
    for _ in range(40):
        p = random.randint(8800, 9800)
        if free(p):
            return p
    return 0


def main():
    port = pick_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    with open(os.path.join(APP_DIR, "runtime.json"), "w") as fh:
        json.dump({"port": srv.server_address[1], "url": url, "pid": os.getpid()}, fh)
    print(url, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
