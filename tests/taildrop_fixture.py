"""用临时文件和本地 Unix 接口模拟守护进程，不接触实际 tailnet。"""

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import socketserver
import threading
from urllib.parse import unquote


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, status, data):
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self):
        fixture = self.server.fixture
        if fixture.denied:
            self.respond(403, b"access denied")
        elif self.path == "/localapi/v0/file-targets":
            self.respond(200, json.dumps(fixture.targets).encode())
        elif self.path == "/localapi/v0/files/":
            self.respond(200, json.dumps([{"Name": name, "Size": len(data)}
                                         for name, data in fixture.files.items()]).encode())
        elif self.path.startswith("/localapi/v0/files/"):
            name = unquote(self.path.rsplit("/", 1)[1])
            fixture.downloads.append(name)
            if name not in fixture.files:
                self.respond(404, b"missing")
                return
            data = fixture.files[name]
            if name in fixture.truncated:
                self.send_response(200)
                self.send_header("Content-Length", str(len(data) + 5))
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True
            else:
                self.respond(200, data)
        else:
            self.respond(404, b"unknown endpoint")

    def do_DELETE(self):
        fixture = self.server.fixture
        name = unquote(self.path.rsplit("/", 1)[1])
        fixture.deletes.append(name)
        if name in fixture.fail_delete:
            fixture.fail_delete.remove(name)
            self.respond(500, b"cleanup temporarily unavailable")
        else:
            fixture.files.pop(name, None)
            self.respond(204, b"")


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class TaildropFixture:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.socket_path = self.directory / "tailscaled.sock"
        self.files = {}
        self.downloads = []
        self.deletes = []
        self.fail_delete = set()
        self.truncated = set()
        self.denied = False
        self.targets = [{"Node": {"Addresses": ["100.64.0.2/32"], "ComputedName": "MacBook Pro",
                                  "Online": True}, "PeerAPIURL": "http://unused"}]
        self.server = Server(str(self.socket_path), Handler)
        self.server.fixture = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.binary = self.directory / "tailscale"
        self.log = self.directory / "sent.jsonl"
        self.delivered = self.directory / "delivered"
        self.delivered.mkdir()
        self.binary.write_text('''#!/usr/bin/python3
import json, pathlib, sys, time
log = pathlib.Path(%r)
delivered = pathlib.Path(%r)
if sys.argv[1:4] != ['file','cp','--update-interval=0'] or sys.argv[-1] != '100.64.0.2:':
    raise SystemExit('unexpected file command')
source = pathlib.Path(sys.argv[4])
with log.open('a') as stream:
    stream.write(json.dumps({'name':source.name, 'args':sys.argv[1:]})+'\\n')
if source.name.startswith('slow'):
    time.sleep(30)
if source.name.startswith('fail'):
    raise SystemExit('simulated transfer failure')
(delivered/source.name).write_bytes(source.read_bytes())
''' % (str(self.log), str(self.delivered)), encoding="utf-8")
        self.binary.chmod(0o755)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
