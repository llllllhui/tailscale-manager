"""文件收发的系统选择器、托盘入口和通知。"""

from pathlib import Path

from gi.repository import Gio, GLib, Gtk

import desktop_settings as settings
from taildrop import TaildropError, TaildropManager


class DesktopFiles:
    def __init__(self, app, menu, binary):
        self.app = app
        downloads = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or str(Path.home() / "Downloads")
        directory = app.preferences.get("receiveDirectory")
        if not isinstance(directory, str) or not Path(directory).is_absolute():
            directory = str(Path(downloads) / "Tailscale")
        self.manager = TaildropManager(binary, directory,
                                      app.preferences.get("autoReceive", True), self.received)
        self.poll_pending = False
        self.receive_requested = False
        self.target_signature = None
        self.changing_auto = False
        self.send_item = app.add_menu_item(menu, "发送文件…")
        app.add_menu_item(menu, "收取待收文件", lambda: self.poll(True))
        app.add_menu_item(menu, "打开接收文件夹", self.open_folder)
        self.auto_item = Gtk.CheckMenuItem.new_with_label("自动接收文件")
        self.auto_item.set_active(self.manager.auto_receive)
        self.auto_item.connect("toggled", self.auto_toggled)
        menu.append(self.auto_item)
        action = Gio.SimpleAction.new("open-received", None)
        action.connect("activate", lambda *_args: self.open_folder())
        app.add_action(action)
        self.poll()
        self.timer = GLib.timeout_add_seconds(5, self.poll)

    def poll(self, receive=False):
        if self.app.stopping:
            return GLib.SOURCE_REMOVE
        if self.poll_pending:
            self.receive_requested = self.receive_requested or receive
        else:
            self.poll_pending = True
            self.app.submit(lambda: self.manager.poll(receive), self.polled)
        return GLib.SOURCE_CONTINUE

    def polled(self, _result):
        self.poll_pending = False
        targets = self.manager.snapshot()["targets"]
        signature = tuple((target["ip"], target["name"], target["online"]) for target in targets)
        if signature != self.target_signature:
            submenu = Gtk.Menu()
            for target in targets:
                item = Gtk.MenuItem.new_with_label(target["name"] + ("" if target["online"] else "（离线）"))
                item.set_sensitive(target["online"])
                item.connect("activate", self.tray_send, target["ip"])
                submenu.append(item)
            if not targets:
                self.app.add_menu_item(submenu, "暂无可接收文件的个人设备").set_sensitive(False)
            submenu.show_all()
            self.send_item.set_submenu(submenu)
            self.target_signature = signature
        if self.receive_requested:
            self.receive_requested = False
            self.poll(True)

    def tray_send(self, _item, ip):
        result = self.send(ip)
        if not result.get("ok"):
            self.app.show_error(result.get("error", "发送失败"))

    def make_chooser(self, title, action):
        self.app.show_window()
        dialog = Gtk.FileChooserDialog(title=title, transient_for=self.app.window, action=action)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL,
                           "发送" if action == Gtk.FileChooserAction.OPEN else "选择文件夹", Gtk.ResponseType.ACCEPT)
        dialog.set_local_only(True)
        return dialog

    def send(self, ip):
        target = next((target for target in self.manager.snapshot()["targets"] if target["ip"] == ip), None)
        if not target or not target["online"]:
            return {"ok": False, "error": "接收设备不可用，请确认是同一账号下已上线的个人设备"}
        dialog = self.make_chooser("发送文件到 " + target["name"], Gtk.FileChooserAction.OPEN)
        dialog.set_select_multiple(True)
        try:
            if dialog.run() != Gtk.ResponseType.ACCEPT:
                return {"ok": True, "cancelled": True}
            paths = dialog.get_filenames()
        finally:
            dialog.destroy()
        try:
            return self.manager.send(paths, ip)
        except (OSError, ValueError, TaildropError) as exc:
            return {"ok": False, "error": str(exc)}

    def save_preferences(self):
        self.app.preferences.update(receiveDirectory=str(self.manager.directory),
                                    autoReceive=self.manager.auto_receive)
        settings.save_settings(self.app.preferences)

    def set_auto_receive(self, enabled):
        self.manager.configure(auto_receive=enabled)
        self.changing_auto = True
        self.auto_item.set_active(enabled)
        self.changing_auto = False
        self.save_preferences()
        if enabled:
            self.poll()

    def auto_toggled(self, item):
        if not self.changing_auto:
            try:
                self.set_auto_receive(item.get_active())
            except OSError as exc:
                self.app.show_error("无法保存接收设置：" + str(exc))

    def choose_directory(self):
        dialog = self.make_chooser("选择文件接收文件夹", Gtk.FileChooserAction.SELECT_FOLDER)
        if self.manager.directory.exists():
            dialog.set_current_folder(str(self.manager.directory))
        try:
            if dialog.run() != Gtk.ResponseType.ACCEPT:
                return {"ok": True, "cancelled": True}
            directory = dialog.get_filename()
        finally:
            dialog.destroy()
        self.manager.configure(directory=directory)
        self.save_preferences()
        return {"ok": True, "message": "已更改接收文件夹"}

    def open_folder(self):
        try:
            self.manager.directory.mkdir(parents=True, exist_ok=True)
            Gio.AppInfo.launch_default_for_uri(self.manager.directory.as_uri(), None)
        except (OSError, GLib.Error) as exc:
            self.app.show_error("无法打开接收文件夹：" + str(exc))

    def received(self, items):
        if not self.app.stopping:
            GLib.idle_add(self.notify_received, items)

    def notify_received(self, items):
        if not self.app.stopping:
            notification = Gio.Notification.new("收到 %d 个文件" % len(items))
            notification.set_body("、".join(item["name"] for item in items[:3]))
            notification.set_default_action("app.open-received")
            self.app.send_notification("taildrop-received", notification)
        return GLib.SOURCE_REMOVE

    def handle(self, path, body, reply):
        try:
            if not isinstance(body, dict):
                raise TaildropError("无效的文件操作")
            if path == "/desktop/files/state":
                result = self.manager.snapshot()
            elif path == "/desktop/files/send":
                result = self.send(body.get("ip"))
            elif path == "/desktop/files/receive":
                self.poll(True)
                result = {"ok": True, "message": "正在检查并收取文件"}
            elif path == "/desktop/files/auto":
                enabled = body.get("enabled")
                if not isinstance(enabled, bool):
                    raise TaildropError("无效的接收设置")
                self.set_auto_receive(enabled)
                result = {"ok": True}
            elif path == "/desktop/files/directory":
                result = self.choose_directory()
            elif path == "/desktop/files/open":
                self.open_folder()
                result = {"ok": True}
            elif path == "/desktop/files/cancel":
                result = self.manager.cancel(body.get("id"))
            else:
                result = {"ok": False, "error": "未知文件操作"}
        except (OSError, ValueError, TaildropError) as exc:
            result = {"ok": False, "error": str(exc)}
        reply(result)

    def close(self):
        GLib.source_remove(self.timer)
        self.manager.close()
