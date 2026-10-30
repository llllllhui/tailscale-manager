"""在真实图形会话运行；使用替身后端，不改变实际 VPN 连接。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

# 独立 D-Bus 测试共享当前显示器，禁用测试子进程的辅助功能桥以免复用会话 socket。
if "--without-tray" in sys.argv:
    os.environ["NO_AT_BRIDGE"] = "1"

SCRIPT = Path(__file__).resolve()
sys.path.insert(0, str(SCRIPT.parents[1]))
import desktop
from desktop import Gdk, GLib, Gtk

desktop.APP_ID += ".Smoke"
secondary = "--secondary" in sys.argv
without_tray = "--without-tray" in sys.argv
fixture = {
    "ok": True, "backendState": "Running", "version": "1.104.1", "tailnet": "example.ts.net",
    "health": [], "authURL": "", "exitNodeIP": "",
    "self": {"hostname": "Ubuntu Desktop", "ipv4": "100.64.0.1", "dnsName": "ubuntu.example.ts.net"},
    "peers": [{"id": "mac", "name": "MacBook Pro", "ip": "100.64.0.2", "os": "macOS",
               "online": True, "direct": True, "relay": "", "exitOption": True},
              {"id": "server", "name": "家庭服务器", "ip": "100.64.0.3", "os": "linux",
               "online": False, "direct": False, "relay": "", "exitOption": False}],
}
actions = []
opened = []
errors = []
original_clipboard = None
clipboard_captured = False


def fake_action(path, body):
    actions.append((path, body))
    if path == "/api/down":
        fixture["backendState"] = "Stopped"
    elif path == "/api/exit-node":
        fixture["exitNodeIP"] = body["ip"]
    return {"ok": True, "message": "完成"}


desktop.backend.build_state = lambda: json.loads(json.dumps(fixture))
desktop.backend.action = fake_action
desktop.backend.TAILSCALE = "/test/tailscale"


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def guarded(function):
    def wrapper(*args):
        try:
            function(*args)
        except Exception as exc:
            errors.append(str(exc))
            print("FAIL:", exc, flush=True)
            screenshot()
            app.quit()
        return GLib.SOURCE_REMOVE
    return wrapper


def later(function, delay=500):
    GLib.timeout_add(delay, guarded(function))


def javascript(script, callback):
    def done(view, result, _data):
        guarded(lambda: callback(view.evaluate_javascript_finish(result)))()
    app.webview.evaluate_javascript(script, -1, None, None, None, done, None)


def screenshot():
    if app.window and app.window.get_visible():
        width, height = app.window.get_window().get_width(), app.window.get_window().get_height()
        picture = Gdk.pixbuf_get_from_window(app.window.get_window(), 0, 0, width, height)
        path = os.environ.get("TS_MANAGER_SCREENSHOT", "/tmp/tailscale-manager-desktop.png")
        picture.savev(path, "png", [], [])


def page_ready():
    global original_clipboard, clipboard_captured
    original_clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text()
    clipboard_captured = True
    javascript("JSON.stringify({bridge:isDesktop,ip:$('ip').textContent,status:$('pillText').textContent})",
               page_checked)


def page_checked(value):
    state = json.loads(value.to_string())
    check(state == {"bridge": True, "ip": "100.64.0.1", "status": "已连接"}, str(state))
    print("PASS: WebKit renders Chinese UI and receives state over the native bridge", flush=True)
    javascript("$('copyBtn').click()", lambda _value: later(copied))


def copied():
    check(Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text() == "100.64.0.1",
          "native clipboard bridge failed")
    javascript("$('toggleBtn').click()", lambda _value: later(disconnected, 3500))


def disconnected():
    check(actions and actions[0][0] == "/api/down", "window action did not reach backend")
    check(app.state["backendState"] == "Stopped", "tray state did not update")
    check(app.toggle_item.get_label() == "连接", "tray connection action is stale")
    submenu = app.exit_item.get_submenu()
    submenu.get_children()[1].set_active(True)
    later(exit_selected, 500)


def exit_selected():
    check(("/api/exit-node", {"ip": "100.64.0.2"}) in actions, "tray exit selection failed")
    check(app.indicator.get_property("connected"), "tray did not register with desktop host")
    app.window.close()
    later(window_hidden)


def window_hidden():
    check(not app.window.get_visible(), "closing window did not leave it in tray")
    global child, child_deadline
    child = subprocess.Popen([sys.executable, str(SCRIPT), "--secondary"])
    child_deadline = time.monotonic() + 6
    later(check_secondary)


def check_secondary():
    if child.poll() is None:
        check(time.monotonic() < child_deadline, "secondary instance did not exit")
        later(check_secondary, 200)
        return
    check(child.returncode == 0, "secondary instance failed")
    check(app.window.get_visible(), "secondary launch did not present original window")
    print("PASS: tray actions, clipboard, close-to-tray, and single-instance activation", flush=True)
    fixture.update(backendState="NeedsLogin", authURL="https://login.tailscale.com/a/smoke")
    app.receive_state(desktop.backend.build_state())
    javascript("refresh().then(() => $('toggleBtn').click()); undefined", lambda _v: later(login_opened))


def login_opened():
    check(opened == ["https://login.tailscale.com/a/smoke"], "login popup was not sent to browser: " + str(opened))
    app.autostart_item.set_active(True)
    check(desktop.settings.autostart_enabled(), "autostart preference did not save")
    app.autostart_item.set_active(False)
    fixture.update(backendState="Running", authURL="")
    app.receive_state(desktop.backend.build_state())
    javascript("refresh(); undefined", lambda _v: later(finish, 2300))


def finish():
    check(not list(Path.cwd().iterdir()), "WebKit wrote runtime files to the working directory")
    screenshot()
    print("PASS: external login, autostart setting, and window capture", flush=True)
    app.quit()


def no_tray():
    check(not app.indicator.get_property("connected"), "isolated session unexpectedly has tray")
    check(app.window and app.window.get_visible(), "background startup must show window without tray")
    app.window.close()
    print("PASS: no-tray session shows window; closing exits", flush=True)


with tempfile.TemporaryDirectory(prefix="tailscale-desktop-smoke-") as temporary:
    if not secondary:
        os.environ["XDG_CONFIG_HOME"] = str(Path(temporary) / "config")
        working = Path(temporary) / "working"
        working.mkdir()
        os.chdir(working)
    app = desktop.TailscaleApplication()
    if os.environ.get("TS_MANAGER_SMOKE_DEBUG"):
        def trace_window(_app, window):
            for event in ("show", "hide", "delete-event"):
                window.connect(event, lambda *_args, event=event: print("WINDOW", event, flush=True) or False)
        app.connect("window-added", trace_window)
    app.open_external = lambda uri: opened.append(uri) or True
    if not secondary:
        def started(_app):
            later(no_tray if without_tray else page_ready, 5500 if without_tray else 2200)
            later(lambda: (_ for _ in ()).throw(AssertionError("smoke test timed out")), 25000)
        app.connect("startup", started)
    result = app.run([sys.argv[0]] + (["--background"] if without_tray else []))
    if clipboard_captured:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        if original_clipboard is not None:
            clipboard.set_text(original_clipboard, -1)
            clipboard.store()
        else:
            clipboard.clear()
    raise SystemExit(1 if errors else result)
