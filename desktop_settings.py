"""用户配置与桌面自启动，不依赖图形环境。"""

import json
import os
from pathlib import Path


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def load_settings():
    try:
        value = json.loads((config_dir() / "tailscale-manager/settings.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(value):
    path = config_dir() / "tailscale-manager/settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def desktop_quote(value):
    # Desktop Entry Exec 的引号规则与 shell 不同。
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace(
        "`", "\\`").replace("$", "\\$").replace("%", "%%") + '"'


def autostart_enabled():
    path = config_dir() / "autostart/tailscale-manager.desktop"
    try:
        text = path.read_text(encoding="utf-8")
        return "Hidden=true" not in text and "X-GNOME-Autostart-enabled=false" not in text
    except OSError:
        return False


def set_autostart(enabled, launcher):
    path = config_dir() / "autostart/tailscale-manager.desktop"
    path.parent.mkdir(parents=True, exist_ok=True)
    if enabled:
        path.write_text(
            "[Desktop Entry]\nType=Application\nName=Tailscale Manager\n"
            "Comment=Tailscale desktop tray\nExec=" + desktop_quote(launcher) + " --background\n"
            "Icon=tailscale-manager\nTerminal=false\n"
            "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=3\n",
            encoding="utf-8",
        )
        legacy = path.with_name("tailscale-systray.desktop")
        if legacy.exists() and "systray" in legacy.read_text(encoding="utf-8"):
            backup = legacy.with_suffix(".desktop.before-manager")
            if not backup.exists():
                backup.write_bytes(legacy.read_bytes())
            legacy.write_text("[Desktop Entry]\nType=Application\nName=Tailscale\nHidden=true\n",
                              encoding="utf-8")
    else:
        # Hidden 也屏蔽可能存在的系统级自启动项。
        path.write_text("[Desktop Entry]\nType=Application\nName=Tailscale Manager\nHidden=true\n",
                        encoding="utf-8")
