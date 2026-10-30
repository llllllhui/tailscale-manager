#!/bin/bash
# 当前用户安装：复制应用文件，不依赖源码目录，也不需要 root。
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
/usr/bin/python3 - "$REPO" "$@" <<'PY'
import os
from pathlib import Path
import shutil
import subprocess
import sys

repo = Path(sys.argv[1])
if len(sys.argv) > 2:
    raise SystemExit("使用方法：bash install.sh（依赖通过 .deb 或 apt 安装）")
try:
    import gi
    for name, version in (("Gtk", "3.0"), ("WebKit2", "4.1"), ("AyatanaAppIndicator3", "0.1")):
        gi.require_version(name, version)
except (ImportError, ValueError) as exc:
    raise SystemExit("缺少桌面组件：%s\n请使用 .deb 安装包，或先执行：\n"
                     "sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 "
                     "gir1.2-ayatanaappindicator3-0.1" % exc)

data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
app = data / "tailscale-manager"
launcher = Path.home() / ".local/bin/tailscale-manager"
if app.resolve() == repo.resolve() and not app.is_symlink():
    raise SystemExit("安装目录不能与源码目录相同")
if app.is_symlink():
    app.unlink()  # 迁移旧版指向源码的软链。
app.mkdir(parents=True, exist_ok=True)
for name in ("desktop.py", "desktop_files.py", "desktop_settings.py", "taildrop.py", "server.py", "ui.html", "icon.svg"):
    shutil.copy2(repo / name, app / name)
for folder in ("icons", "bin"):
    shutil.copytree(repo / folder, app / folder, dirs_exist_ok=True)
(app / "bin/tailscale-manager").chmod(0o755)
launcher.parent.mkdir(parents=True, exist_ok=True)
if launcher.is_symlink():
    launcher.unlink()
elif launcher.exists():
    backup = launcher.with_name("tailscale-manager.before-desktop")
    if not backup.exists():
        shutil.copy2(launcher, backup)
launcher.write_text('#!/bin/sh\nexec ' + "'" + str(app / "bin/tailscale-manager").replace(
    "'", "'\\''") + "' \"$@\"\n", encoding="utf-8")
launcher.chmod(0o755)

sys.path.insert(0, str(app))
from desktop_settings import desktop_quote
entries = data / "applications"
entries.mkdir(parents=True, exist_ok=True)
desktop = (repo / "packaging/tailscale-manager.desktop").read_text(encoding="utf-8")
desktop = desktop.replace("Exec=/usr/bin/tailscale-manager", "Exec=" + desktop_quote(launcher))
(entries / "io.github.tailscalemanager.Manager.desktop").write_text(desktop, encoding="utf-8")
legacy_entry = entries / "tailscale-manager.desktop"
if legacy_entry.exists() and "tailscale-manager" in legacy_entry.read_text(encoding="utf-8"):
    legacy_entry.unlink()
icons = data / "icons/hicolor/scalable/apps"
icons.mkdir(parents=True, exist_ok=True)
shutil.copy2(repo / "icon.svg", icons / "tailscale-manager.svg")
for tool, arguments in (("update-desktop-database", [str(entries)]),
                        ("gtk-update-icon-cache", ["-f", "-t", str(data / "icons/hicolor")])):
    if shutil.which(tool):
        subprocess.run([tool] + arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("已安装到：", app)
print("从应用列表搜索「Tailscale 管理器」即可打开。")
PY
