#!/bin/bash
# 使用系统打包工具生成可通过 Ubuntu 软件安装器安装的 .deb。
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/lib/tailscale-manager/icons" \
  "$STAGE/usr/bin" "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/doc/tailscale-manager"
install -m644 "$REPO/packaging/control" "$STAGE/DEBIAN/control"
for file in desktop.py desktop_settings.py server.py ui.html icon.svg; do
  install -m644 "$REPO/$file" "$STAGE/usr/lib/tailscale-manager/$file"
done
install -m644 "$REPO"/icons/*.svg "$STAGE/usr/lib/tailscale-manager/icons/"
install -m755 "$REPO/bin/tailscale-manager" "$STAGE/usr/bin/tailscale-manager"
install -m644 "$REPO/packaging/tailscale-manager.desktop" \
  "$STAGE/usr/share/applications/io.github.tailscalemanager.Manager.desktop"
install -m644 "$REPO/icon.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/tailscale-manager.svg"
install -m644 "$REPO/README.md" "$STAGE/usr/share/doc/tailscale-manager/README.md"
SIZE="$(du -sk "$STAGE/usr" | cut -f1)"
printf 'Installed-Size: %s\n' "$SIZE" >> "$STAGE/DEBIAN/control"
mkdir -p "$REPO/dist"
find "$STAGE" -type d -exec chmod 755 {} +
dpkg-deb --root-owner-group --build "$STAGE" "$REPO/dist/tailscale-manager_0.2.0_all.deb"
printf '\n安装包：%s/dist/tailscale-manager_0.2.0_all.deb\n' "$REPO"
