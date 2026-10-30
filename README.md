# Tailscale Manager

非官方的 Linux Tailscale 图形管理器：独立窗口、顶栏托盘、Taildrop 文件收发、可选登录自启动。
关闭窗口后仍在托盘运行，重复启动会唤回已有窗口。

![Tailscale Manager 桌面界面（演示数据）](docs/screenshot.png)

## 安装

面向 Ubuntu 24.04 / Debian 系桌面。从 [最新发布版本](https://github.com/llllllhui/tailscale-manager/releases/latest)
下载 `tailscale-manager_0.3.0_all.deb`，然后在下载目录执行：

```bash
sudo apt install ./tailscale-manager_0.3.0_all.deb
```

安装器会安装 GTK、WebKit 和 Ayatana 托盘组件。随后在应用列表搜索
**Tailscale 管理器**，或运行 `tailscale-manager`。

Tailscale 本身需要事先安装并启动；支持系统包和 Homebrew 安装的 CLI。
本应用沿用已有 Tailscale 登录和网络设置。

## 日常使用

- **窗口**：连接／断开、复制 IP、查看设备、切换出口节点、测延迟。
- **托盘菜单**：查看状态、连接／断开、复制 IP、切换出口节点、发送文件和打开接收文件夹。
- **登录后自动启动**：在托盘或窗口右上角菜单中勾选，后台启动不弹出窗口。
- **关闭窗口 / Ctrl+W**：隐藏到托盘。桌面不支持托盘时正常退出。
- **退出管理器 / Ctrl+Q**：退出图形应用，Tailscale 的网络连接继续由守护进程维持。
- **需要登录时**：点击登录按钮，在默认浏览器中完成授权。

## 文件收发

- **发送**：在设备旁点击「发送文件」，在系统文件窗口中选择一个或多个普通文件。文件夹请先压缩。
- **发送状态**：本次传输列表显示排队、发送中、完成、失败和取消；大文件在后台传输，不受普通命令超时限制。
- **取消**：列表中可取消排队中或发送中的文件，连接管理仍然可用。
- **自动接收**：默认保存到系统下载目录下的 `Tailscale` 文件夹。收到文件后显示系统通知，点击通知打开接收文件夹。
- **手动接收**：关闭「自动接收文件」后，点「立即收取」保存待收文件；托盘也提供收取入口。
- **接收位置**：点击「更改」选择文件夹；窗口和托盘都可以打开接收文件夹。
- **同名文件**：自动保存为 `文件名 (1).扩展名` 等名称，不覆盖已有文件。完整保存并同步后才清理 Tailscale 收件箱；中断时保留原件以便重试。
- **记录**：本次运行的最近 50 条已结束传输和全部未完成发件保留在内存，退出后清除。

Taildrop 仅支持同一账号下的个人设备，不能向其他用户或带标签的设备发送。
请在 [Tailscale 控制台的 General 设置](https://login.tailscale.com/admin/settings/general) 中启用 **Send Files**，
并保持两端 Tailscale 在线。不可发送的设备会禁用按钮。
macOS 接收端还需按 [Taildrop 官方说明](https://tailscale.com/docs/features/taildrop) 启用 Tailscale 的 Sharing 扩展。

开启本应用自启动时，会备份并停用旧的
`~/.config/autostart/tailscale-systray.desktop`，避免下次登录出现两个托盘。
备份文件为 `tailscale-systray.desktop.before-manager`；如要恢复官方托盘，可复制回原文件名。

如果普通用户没有控制权限，界面会给出具体提示。一次性授权示例：

```bash
sudo /home/linuxbrew/.linuxbrew/bin/tailscale set --operator="$USER"
```

请使用本机实际的 Tailscale 路径；不要将 Homebrew 二进制软链到 `/usr/local/bin`。

## 从源码运行与打包

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 gir1.2-ayatanaappindicator3-0.1
./bin/tailscale-manager
bash build-deb.sh
```

安装包生成在 `dist/`，可以使用 `sudo apt install ./dist/tailscale-manager_0.3.0_all.deb` 安装。

GNOME 桌面还需要托盘扩展（Ubuntu 默认通常已有）：

```bash
sudo apt install gnome-shell-extension-appindicator
```

也可在依赖齐全后执行 `bash install.sh`，安装到当前用户的
`~/.local/share/tailscale-manager` 和 `~/.local/bin`。
用户安装会复制实际文件，安装后不再依赖源码目录。
更新用户安装时重新执行安装脚本。

卸载系统安装：`sudo apt remove tailscale-manager`。
卸载用户安装：先退出管理器，再删除 `~/.local/share/tailscale-manager`、
`~/.local/bin/tailscale-manager`、`~/.local/share/applications/io.github.tailscalemanager.Manager.desktop`
和 `~/.local/share/icons/hicolor/scalable/apps/tailscale-manager.svg`。
两种安装的用户设置位于 `~/.config/tailscale-manager`，自启动项位于
`~/.config/autostart/tailscale-manager.desktop`。卸载后可一并删除。
路径遵循 XDG 配置，若自定义了 XDG 环境变量，以对应目录为准。

## 结构

| 文件 | 用途 |
| --- | --- |
| `desktop.py` | GTK 单实例应用、WebKit 消息桥、托盘与窗口生命周期 |
| `desktop_settings.py` | 窗口大小、自启动及旧托盘配置迁移 |
| `desktop_files.py` | 系统文件选择器、收件设置、托盘入口与通知 |
| `taildrop.py` | 文件目标发现、发送队列、取消和流式接收 |
| `server.py` | Tailscale CLI 状态与操作；也保留开发用 HTTP 服务 |
| `ui.html` | 中文管理界面、深色模式、窗口尺寸适配 |
| `packaging/` | Debian 包元信息和桌面入口 |
| `build-deb.sh` | 生成 `dist/tailscale-manager_0.3.0_all.deb` |
| `install.sh` | 当前用户安装器 |

桌面版不监听 HTTP 端口。界面通过 WebKit 消息桥访问应用，CLI 操作在工作线程执行，
退出应用时一并结束。文件收发复用既有 Tailscale 守护进程和本地 Unix socket，文件不经由管理器的 HTTP 服务。
安装目录可只读，运行时不在源码或安装目录写文件。

`python3 server.py` 仍可启动旧开发用 HTTP 界面。它只绑定 `127.0.0.1`，
**没有本地用户鉴权**，仅用于可信的单用户开发环境；桌面版不使用该服务。

## 验证

```bash
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 tests/smoke_desktop.py
# 无托盘宿主场景（仍需要可用的图形显示器）：
dbus-run-session -- /usr/bin/python3 tests/smoke_desktop.py --without-tray
```

文件测试通过临时 Unix socket 和测试命令验证实际字节、同名保护、接收中断、重试和取消。
桌面检查还验证文件选择、发送、自动及手动接收、目录设置和通知；使用临时文件及替身设备，
不会切换实际 VPN 或向真实设备发送文件，并保留原有剪贴板。默认保存窗口截图到 `/tmp/tailscale-manager-desktop.png`。
