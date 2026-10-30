#!/usr/bin/python3
"""Tailscale Manager：独立窗口、状态托盘与 WebKit 消息桥。"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import signal
import sys
from urllib.parse import urlparse

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
    from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2
except (ImportError, ValueError) as exc:
    message = ("桌面组件未安装完整，请安装 Tailscale Manager 的 .deb 安装包，或执行：\n"
               "sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 "
               "gir1.2-ayatanaappindicator3-0.1\n\n" + str(exc))
    print(message, file=sys.stderr)
    import shutil
    import subprocess
    if shutil.which("zenity"):
        subprocess.run(["zenity", "--error", "--title=Tailscale Manager", "--text=" + message])
    raise SystemExit(1)

import desktop_settings as settings
from desktop_files import DesktopFiles
import server as backend

APP_DIR = Path(__file__).resolve().parent
APP_ID = "io.github.tailscalemanager.Manager"
LAUNCHER = (Path("/usr/bin/tailscale-manager") if str(APP_DIR) == "/usr/lib/tailscale-manager"
            else APP_DIR / "bin/tailscale-manager")


class TailscaleApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        for option, description in (("background", "Start in the system tray"),
                                    ("quit", "Quit the running manager")):
            self.add_main_option(option, 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                                 description, None)
        self.window = None
        self.webview = None
        self.state = {"ok": False, "loading": True, "error": "正在读取 Tailscale 状态…"}
        self.state_pending = False
        self.action_busy = False
        self.stopping = False
        self.exit_signature = None
        self.preferences = settings.load_settings()

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="tailscale")
        self.indicator = AppIndicator.Indicator.new(
            "tailscale-manager", str(APP_DIR / "icons/tailscale-disconnected.svg"),
            AppIndicator.IndicatorCategory.COMMUNICATIONS)
        self.indicator.set_title("Tailscale Manager")
        self.menu = Gtk.Menu()
        self.status_item = self.add_menu_item(self.menu, "正在读取状态…")
        self.status_item.set_sensitive(False)
        self.ip_item = self.add_menu_item(self.menu, "复制本机 IP", self.copy_ip)
        self.toggle_item = self.add_menu_item(self.menu, "连接", self.toggle_connection)
        self.exit_item = self.add_menu_item(self.menu, "出口节点")
        self.menu.append(Gtk.SeparatorMenuItem())
        self.files = DesktopFiles(self, self.menu, backend.TAILSCALE)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.add_menu_item(self.menu, "打开 Tailscale Manager", self.show_window)
        self.autostart_item = Gtk.CheckMenuItem.new_with_label("登录后自动启动")
        self.autostart_item.set_active(settings.autostart_enabled())
        self.autostart_item.connect("toggled", self.change_autostart)
        self.menu.append(self.autostart_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.add_menu_item(self.menu, "退出管理器", self.quit)
        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        for name, callback in (("show", self.show_window), ("hide", self.hide_window),
                               ("quit", self.quit)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, fn=callback: fn())
            self.add_action(action)
        self.set_accels_for_action("app.hide", ["<Primary>w"])
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        self.refresh_state()
        self.poll_source = GLib.timeout_add_seconds(3, self.refresh_state)
        # 托盘宿主注册需要一点时间；不可用时展示窗口，避免后台无法唤回。
        self.tray_source = GLib.timeout_add_seconds(4, self.check_tray)

    @staticmethod
    def add_menu_item(menu, label, callback=None):
        item = Gtk.MenuItem.new_with_label(label)
        if callback:
            item.connect("activate", lambda _item: callback())
        menu.append(item)
        return item

    def do_command_line(self, command_line):
        options = command_line.get_options_dict()
        if options.contains("quit"):
            self.quit()
        elif not options.contains("background"):
            self.activate()
        return 0

    def do_activate(self):
        self.show_window()

    def submit(self, function, callback):
        def completed(future):
            try:
                result = future.result()
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            if not self.stopping:
                GLib.idle_add(deliver, result)

        def deliver(result):
            if not self.stopping:
                callback(result)
            return GLib.SOURCE_REMOVE

        self.executor.submit(function).add_done_callback(completed)

    def refresh_state(self):
        if not self.stopping and not self.state_pending:
            self.state_pending = True
            self.submit(backend.build_state, self.receive_state)
        return GLib.SOURCE_CONTINUE

    def receive_state(self, state):
        self.state_pending = False
        self.state = state
        self.update_tray()

    def update_tray(self):
        running = self.state.get("backendState") == "Running"
        status = {"Running": "已连接", "Stopped": "已断开", "NeedsLogin": "需要登录",
                  "NeedsMachineAuth": "等待设备审批", "Starting": "正在连接"}.get(
                      self.state.get("backendState"), "服务不可用")
        self.status_item.set_label("Tailscale · " + status)
        icon = "tailscale-connected.svg" if running else "tailscale-disconnected.svg"
        self.indicator.set_icon_full(str(APP_DIR / "icons" / icon), status)
        ip = self.state.get("self", {}).get("ipv4", "")
        self.ip_item.set_label("复制 IP · " + ip if ip else "暂无本机 IP")
        self.ip_item.set_sensitive(bool(ip))
        self.toggle_item.set_label("断开连接" if running else
                                   "登录 Tailscale…" if status == "需要登录" else "连接")
        self.toggle_item.set_sensitive(not self.action_busy and bool(backend.TAILSCALE))
        peers = [p for p in self.state.get("peers", []) if p.get("exitOption")]
        selected = self.state.get("exitNodeIP", "")
        signature = (selected, tuple((p["ip"], p["name"], p["online"]) for p in peers))
        self.exit_item.set_sensitive(not self.action_busy and self.state.get("ok", False))
        if signature != self.exit_signature:
            submenu = Gtk.Menu()
            group = None
            for ip, label, online in [("", "不使用出口节点", True)] + [
                    (p["ip"], p["name"] + ("" if p["online"] else "（离线）"), p["online"])
                    for p in peers]:
                item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
                group = group or item
                item.set_active(ip == selected)
                item.set_sensitive(online or ip == selected)
                item.connect("toggled", self.select_exit, ip)
                submenu.append(item)
            if not peers:
                self.add_menu_item(submenu, "没有可用出口节点").set_sensitive(False)
            submenu.show_all()
            self.exit_item.set_submenu(submenu)
            self.exit_signature = signature

    def select_exit(self, item, ip):
        if item.get_active() and ip != self.state.get("exitNodeIP", ""):
            self.perform_action("/api/exit-node", {"ip": ip}, self.tray_result)

    def toggle_connection(self):
        if self.state.get("backendState") == "NeedsLogin" and self.state.get("authURL"):
            self.open_external(self.state["authURL"])
        else:
            path = "/api/down" if self.state.get("backendState") == "Running" else "/api/up"
            self.perform_action(path, {}, self.tray_result)

    def perform_action(self, path, body, callback):
        if self.action_busy:
            callback({"ok": False, "error": "正在处理上一项操作，请稍候"})
            return
        self.action_busy = True
        self.update_tray()

        def done(result):
            self.action_busy = False
            self.update_tray()
            callback(result)
            self.refresh_state()

        self.submit(lambda: backend.action(path, body), done)

    def tray_result(self, result):
        if result.get("ok") and result.get("url"):
            self.open_external(result["url"])
        elif not result.get("ok"):
            self.show_error(result.get("error", "操作失败") + "\n" + result.get("hint", ""))

    def copy_ip(self):
        ip = self.state.get("self", {}).get("ipv4", "")
        if not ip:
            return {"ok": False, "error": "暂无本机 IP"}
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(ip, -1)
        clipboard.store()
        return {"ok": True, "message": "已复制 " + ip}

    def change_autostart(self, item):
        try:
            settings.set_autostart(item.get_active(), LAUNCHER)
        except OSError as exc:
            item.handler_block_by_func(self.change_autostart)
            item.set_active(settings.autostart_enabled())
            item.handler_unblock_by_func(self.change_autostart)
            self.show_error("无法保存自启动设置：" + str(exc))

    def show_window(self):
        if not self.window:
            self.window = Gtk.ApplicationWindow(application=self, title="Tailscale Manager")
            self.window.set_icon_from_file(str(APP_DIR / "icon.svg"))
            self.window.set_default_size(self.dimension("width", 900, 640, 1800),
                                         self.dimension("height", 700, 480, 1200))
            self.window.set_size_request(640, 480)
            self.window.connect("delete-event", self.on_close)
            header = Gtk.HeaderBar(title="Tailscale Manager", show_close_button=True)
            button = Gtk.MenuButton()
            button.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
            button.set_tooltip_text("应用菜单")
            # 同一菜单在窗口与托盘都能访问，托盘不可用时仍可退出和设置自启动。
            button.set_popup(self.menu)
            header.pack_end(button)
            self.window.set_titlebar(header)
            manager = WebKit2.UserContentManager()
            manager.register_script_message_handler("desktop")
            manager.connect("script-message-received::desktop", self.on_message)
            self.webview = WebKit2.WebView(
                web_context=WebKit2.WebContext.new_ephemeral(), user_content_manager=manager)
            self.webview.get_settings().set_enable_developer_extras(False)
            self.webview.connect("decide-policy", self.navigation_policy)
            self.webview.connect("context-menu", lambda *_args: True)
            self.webview.connect("permission-request", self.deny_permission)
            self.webview.connect("web-process-terminated", self.web_process_terminated)
            self.window.add(self.webview)
            self.load_ui()
        self.window.show_all()
        self.window.present()

    def dimension(self, name, default, minimum, maximum):
        value = self.preferences.get(name, default)
        return max(minimum, min(maximum, value)) if isinstance(value, int) else default

    def load_ui(self):
        text = (APP_DIR / "ui.html").read_text(encoding="utf-8")
        # 数据位于 application/json script 中，转义 '<' 防止设备名提前结束标签。
        boot = json.dumps(self.state, ensure_ascii=False).replace("<", "\\u003c")
        self.webview.load_html(text.replace("/*__BOOT__*/null", boot), APP_DIR.as_uri() + "/")

    def on_message(self, _manager, message):
        try:
            request = json.loads(message.get_js_value().to_string())
            identifier = request["id"]
            if not isinstance(identifier, int):
                return
            path = request["path"]
            callback = lambda result: self.reply(identifier, result)
            if path == "/api/state":
                callback(self.state)
            elif path == "/desktop/copy":
                callback(self.copy_ip())
            elif isinstance(path, str) and path.startswith("/desktop/files/"):
                self.files.handle(path, request.get("body") or {}, callback)
            elif path == "/desktop/open":
                body = request.get("body") or {}
                uri = body.get("url", "") if isinstance(body, dict) else ""
                opened = isinstance(uri, str) and self.open_external(uri)
                callback({"ok": bool(opened), "error": "无法打开登录页面" if not opened else ""})
            elif path in ("/api/up", "/api/down", "/api/login", "/api/exit-node", "/api/ping"):
                self.perform_action(path, request.get("body", {}), callback)
            else:
                callback({"ok": False, "error": "未知操作"})
        except (ValueError, KeyError, TypeError, AttributeError):
            return

    def reply(self, identifier, result):
        if self.webview:
            script = "window.__desktopReply(%s,%s)" % (identifier, json.dumps(result))
            self.webview.evaluate_javascript(script, -1, None, None, None, None, None)

    def open_external(self, uri):
        if urlparse(uri).scheme in ("http", "https"):
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
                return True
            except GLib.Error as exc:
                self.show_error("无法打开浏览器：" + str(exc))
        return False

    def navigation_policy(self, _view, decision, kind):
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                    WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri not in ("about:blank", APP_DIR.as_uri() + "/"):
                decision.ignore()
                self.open_external(uri)
                return True
        return False

    @staticmethod
    def deny_permission(_view, request):
        request.deny()
        return True

    def web_process_terminated(self, _view, _reason):
        self.show_error("界面意外停止，关闭提示后将重新加载。")
        self.load_ui()

    def check_tray(self):
        if not self.indicator.get_property("connected"):
            self.show_window()
        return GLib.SOURCE_REMOVE

    def hide_window(self):
        if self.window:
            self.save_window_size()
            if self.indicator.get_property("connected"):
                self.window.hide()
            else:
                self.quit()

    def on_close(self, _window, _event):
        self.hide_window()
        return True

    def save_window_size(self):
        if self.window and not self.window.is_maximized():
            width, height = self.window.get_size()
            self.preferences.update(width=width, height=height)
            try:
                settings.save_settings(self.preferences)
            except OSError as exc:
                print("无法保存窗口大小：", exc, file=sys.stderr)

    def show_error(self, message):
        self.show_window()
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE, text="Tailscale Manager")
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def do_shutdown(self):
        self.stopping = True
        self.save_window_size()
        GLib.source_remove(self.poll_source)
        # 定时器可能已运行完毕；不再尝试移除 tray_source。
        self.indicator.set_status(AppIndicator.IndicatorStatus.PASSIVE)
        self.files.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        Gtk.Application.do_shutdown(self)


def main():
    GLib.set_prgname("tailscale-manager")
    GLib.set_application_name("Tailscale Manager")
    app = TailscaleApplication()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: app.quit() or False)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: app.quit() or False)
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
