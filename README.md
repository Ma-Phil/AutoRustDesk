# AutoRustDesk

**插上网线，一键控制设备桌面：不用键盘、鼠标和显示器，也不依赖 WiFi 和局域网。**

## 为什么做这个

这个项目来自调试无人机。无人机的机载电脑装的是 Ubuntu 20.04，以前每次调试只有两种办法：

- **接外设**：给无人机接上键盘、鼠标和便携显示器，东西多，接线也麻烦。
- **走无线**：用 RustDesk 这类远程桌面，但无人机得先连进同一个 WiFi。配置 WiFi 本身就要接显示器；而且 WiFi 环境经常变（实验室局域网、校园网……），一换地方就连不上，又得接显示器。

网线是最稳定的连接：插上就通，不管在哪里、周围有什么网络都一样。AutoRustDesk 把插上网线之后要做的事全部自动化：配置直连网络、找到设备、离线安装并配置 RustDesk、打开远程桌面。

- **不用带外设**：不需要键盘、鼠标和便携显示器，设备没接显示器也能看到桌面。
- **不依赖网络环境**：不需要 WiFi、路由器、校园网或互联网。
- **插上就连**：自动配置网络、自动找到设备，配置过的设备几秒钟就能连上。
- **设备零准备**：设备上没装 RustDesk 也可以，程序会离线装好、配好；同型号设备通用。

## 它做了什么

用一根网线把电脑 A（本机，有屏幕）和电脑 B（被控设备：Ubuntu 20.04 / 22.04 / 24.04，可以没有显示器）连起来，点一下「一键连接」，程序会自动完成以下步骤：

1. **配置直连网络**：给 A 的网卡设直连网段，并临时运行 DHCP 服务。不设网关、不改 DNS，不影响 A 的 Wi-Fi 上网，也不影响 B 原有的网络。
2. **找到 B**：B 用 DHCP 时直接看分配记录；B 是固定 IP 时，通过 IPv6 链路本地地址、被动监听和主动 ARP 扫描找到它。
3. **SSH 登录 B 并检查环境**：包括系统、架构、RustDesk 版本、登录界面、显示器、会话等。
4. **离线安装 RustDesk**：B 没装或版本较旧时，从离线部署包里取出与 B 的 Ubuntu 版本对应的那部分，上传后用本地软件源安装。B 不需要联网。
5. **配置 RustDesk 和 B 的系统**：开启 IP 直连、设置固定密码、免人工确认、IP 白名单；登录界面改用 Xorg；没接显示器时启用虚拟显示器；禁止自动休眠。
6. **验证并连接**：在 A 上自动打开 RustDesk，以"IP + 密码"连接 B。B 没有登录时看到的是 B 的登录界面，输入 B 的系统密码即可。

![主窗口（示意）](docs/images/main-window.png)

> 电脑 A 支持 **Ubuntu（及其它 Linux 桌面）、Windows 10/11、Apple 芯片的 Mac（macOS 13 及以上）**。
> Windows 和 macOS 版是新加入的：在 GitHub Actions 的真实 Windows / macOS 系统上做了自动化测试，但还没有接真实的电脑 B 验证过，遇到问题欢迎反馈。
> 需求和设计见 [docs/requirements.md](docs/requirements.md)、[docs/design.md](docs/design.md)。

---

## 为什么这样设计

- **用 IP 直连，不用 RustDesk ID**：网线直连时 B 连不上 RustDesk 的公共服务器，按 ID 是连不上的。RustDesk 自带"IP 直连"：B 监听 TCP 21118，A 直接输入 B 的 IP 就能连，不需要任何服务器。
- **离线部署包 = 官方原版 deb + 完整依赖 + 安装配置脚本**，不重新编译 RustDesk。原因有三：
  - 服务自启、登录界面、虚拟显示器等都是 B 的系统配置，本来就打不进程序包；
  - RustDesk 的固定密码以加盐哈希存储，无法预置在文件里；
  - 自己编译要维护 Rust/Flutter 工具链，还要承担 AGPL 义务。
- **没接显示器**：新版 RustDesk 已移除 `allow-linux-headless` 无头模式，本程序自己配置虚拟显示器（`xserver-xorg-video-dummy`）。开机时会自动判断：接了显示器就用真实显示器，没接就用虚拟显示器。

## 环境要求

| | 要求 |
|---|---|
| 电脑 A | 以下任一系统，有有线网口或 USB 网卡（RustDesk 客户端在完整离线版里已经带了）：<br>· Ubuntu 20.04 及以上（x86_64）桌面版<br>· Windows 10 / 11（x64），建议安装 [Npcap](https://npcap.com)<br>· Apple 芯片（M1 及以后）的 Mac，macOS 13 及以上 |
| 电脑 B | Ubuntu 20.04 / 22.04 / 24.04 桌面版，x86_64；**已开启 SSH**；有一个可以 sudo 的账号。26.04 只能部分支持，见[常见问题](#常见问题) |
| 网线 | 普通网线即可（现代网卡自动识别直连/交叉） |
| 自己制作离线包（可选） | 任意一台能上网的电脑（Linux / Windows / macOS 都可以，只需做一次） |

## 快速开始

### 第 1 步：在电脑 A 上安装

在仓库的 [Releases](https://github.com/Ma-Phil/AutoRustDesk/releases) 页面下载对应系统的**完整离线版**。里面已经带了：

- 本程序；
- 电脑 B 的离线部署包：RustDesk 官方 deb、Ubuntu 20.04 / 22.04 / 24.04 下的全部依赖、虚拟显示驱动；
- 电脑 A 用的 RustDesk 官方客户端。

装好以后，电脑 A 全程不用联网。每个安装包约 400~500 MB，其中离线部署包约 300 MB。

| 电脑 A | 下载 | 安装 |
|---|---|---|
| Windows 10 / 11 | `AutoRustDesk-<版本>-windows-x64-setup.exe` | 双击安装，不需要管理员权限 |
| Mac（Apple 芯片） | `AutoRustDesk-<版本>-macos-arm64.dmg` | 打开后把 AutoRustDesk 和 RustDesk 都拖进「应用程序」 |
| Ubuntu 20.04 及以上 | `autorustdesk_<版本>_amd64.deb` | `sudo apt install ./autorustdesk_<版本>_amd64.deb`，然后在应用菜单里打开 |
| Ubuntu 20.04 及以上 | `AutoRustDesk-<版本>-x86_64.AppImage` | 不用安装：`chmod +x` 后直接运行 |

- **Windows**：程序没有数字签名，第一次运行时 Windows 可能提示"已保护你的电脑"，点「更多信息 → 仍要运行」。
  建议另外装一下 [Npcap](https://npcap.com)（Wireshark 用的同一个抓包驱动，它的许可证不允许我们打包进来）。不装也能用：B 用 DHCP（Ubuntu 默认）或开着 IPv6 时都能找到；只有 B 是固定 IP、又关闭了 IPv6 时才必须装。
- **macOS**：程序没有经过 Apple 公证，第一次打开会提示"无法验证开发者"：
  - macOS 15 及以上：先打开一次，然后到「系统设置 → 隐私与安全性」，点「仍要打开」；
  - macOS 14 及以下：在访达里右键点 `AutoRustDesk.app` →「打开」；
  - 或者在终端执行 `xattr -dr com.apple.quarantine /Applications/AutoRustDesk.app`。
- **已经装了 RustDesk**：程序优先用本机已安装的 RustDesk，找不到时才用自带的。
- 最新的开发版在 [Actions](https://github.com/Ma-Phil/AutoRustDesk/actions) 里：打开最近一次成功的"测试与打包"，在页面底部的 Artifacts 下载（需要登录 GitHub）。

**权限**：程序以普通用户身份运行。只有配置网卡、运行 DHCP 这类网络操作，会在点「一键连接」时通过系统授权以管理员权限执行，每次启动授权一次：Ubuntu 弹出系统授权框（pkexec），Windows 弹出 UAC 确认框，macOS 弹出管理员密码框。

**本机网络会怎样变化**：只改插网线的那个网口，不设网关、不改 DNS，Wi-Fi 上网不受影响；关闭程序或点「恢复本机网络」后复原。
- Ubuntu：临时新建一个 NetworkManager 连接，结束后删除，自动切回原来的连接；
- macOS：在网口上临时追加一个地址，不修改「系统设置」里的网络配置；
- Windows：网口原来是"自动获得 IP 地址"时，临时改为固定地址 192.168.77.1，结束后改回自动获得。

### 第 2 步：连接电脑 B

1. 用网线连接 A 和 B，确认 B 已开机（网口灯亮）。
2. 在「直连网卡」里选择插了网线的网口（一般会自动选好）。「离线包」默认用程序自带的，不用选。
3. 点「**一键连接**」。
   - 弹出系统授权框时，输入 A 的密码（Windows 上点「是」）。
   - 弹出登录框时，输入 B 的 SSH 账号和密码。同型号设备通常账号相同，可以勾选「记住密码」。
4. 完成后自动打开 RustDesk 窗口。B 没有登录时，先看到 B 的登录界面，输入 B 的系统密码登录即可。
5. 用完后关闭程序，或点「恢复本机网络」，A 的网卡会恢复原来的设置。

以后再连同一台设备，点「**快速连接**」即可。它会跳过安装和配置，几秒钟就能连上。

连完一台想换下一台：直接拔线换插另一台 B，再点「一键连接」，不用关闭程序。

## 自己制作离线包（可选）

完整离线版已经自带离线包。想换一个 RustDesk 版本、加上 Ubuntu 26.04，或者用源码运行本程序时，可以自己制作（需要联网，做一次即可）：

1. 在 [RustDesk Releases](https://github.com/rustdesk/rustdesk/releases) 下载 `rustdesk-<版本>-x86_64.deb`。
2. 用图形界面：菜单「文件 → 制作离线包…」，选择 deb，**勾选电脑 B 可能的 Ubuntu 版本**，选一个软件源（国内推荐清华 TUNA），点「开始制作」。
   也可以用命令行：
   ```bash
   python3 -m autorustdesk bundle build ~/下载/rustdesk-1.4.2-x86_64.deb \
       --ubuntu 20.04,22.04,24.04 --mirror https://mirrors.tuna.tsinghua.edu.cn/ubuntu
   ```
3. 得到 `autorustdesk-bundle-rustdesk-<版本>-ubuntu-20.04-22.04-24.04.tar`。每个 Ubuntu 版本约 100 MB，三个版本约 300 MB。里面包含：
   - RustDesk 本身；
   - 各版本下的全部依赖，每个版本约 250 个包，相同的文件只存一份；
   - 虚拟显示驱动。

   连接时只上传 B 那个版本需要的部分。每个包都会按软件源索引校验 SHA256；本机有 `gpgv` 和 Ubuntu 密钥环时，还会校验软件源签名。

同一个离线包可以给所有同型号设备使用。

做好后在「离线包」里选择它，就会代替自带的离线包。

## 从源码运行 / 自己打包（可选）

源码运行（三个系统都可以，需要 Python 3.8 以上；源码运行时没有自带的离线包和 RustDesk 客户端）：

```bash
sudo apt install python3-venv libxcb-cursor0      # Ubuntu
python3 -m venv .venv
.venv/bin/pip install --upgrade pip               # Ubuntu 20.04 自带的 pip 太旧，装不上新版 PySide6
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m autorustdesk
```

自己打包：

```bash
# 下载 RustDesk 官方安装包（latest 为最新正式版，也可以写版本号）
python packaging/fetch_rustdesk.py latest deb dl/          # 电脑 B 用的 deb，用来制作离线包
python packaging/fetch_rustdesk.py latest windows dl/      # 电脑 A 用：windows / macos-arm64 / appimage

# Windows / macOS（在对应系统上运行；Windows 需要 Inno Setup 6 才能生成安装程序）
pip install --prefer-binary -r requirements.txt pyinstaller
python packaging/build.py --bundle 离线包.tar --client dl/rustdesk-<版本>-x86_64.exe

# Ubuntu（建议在 Ubuntu 20.04 上打包，打出来的程序在 20.04 及以后的版本上都能运行）
bash packaging/build_linux.sh
python3 packaging/linux_packages.py --bundle 离线包.tar --client dl/rustdesk-<版本>-x86_64.AppImage
```

`build_linux.sh` 打包前会检查打包机上的系统库，缺什么就打印对应的安装命令（例如 `sudo apt install libxcb-cursor0`）。这些库会一起打进程序包，所以别的电脑上不需要再装。

## 程序对 B 做了哪些修改

所有修改都会记录在 B 的 `/etc/autorustdesk/state.json` 里，修改前的原文件备份为 `*.autorustdesk.bak`。

| 修改 | 目的 | 位置 |
|---|---|---|
| 安装 `rustdesk`、`xserver-xorg-video-dummy` 及缺少的依赖 | 远程控制、虚拟显示器 | apt（使用本地源，`--no-remove`，不会删除任何包） |
| RustDesk 选项：`direct-server=Y`、`direct-access-port=21118`、`verification-method=use-permanent-password`、`approve-mode=password`、`whitelist=<直连网段>` | IP 直连、无人值守、只允许直连网段连接 | `rustdesk --option` |
| RustDesk 固定密码 | 无人值守连接 | `rustdesk --password` |
| `WaylandEnable=false`（并删除被注释掉的那一行） | 让 RustDesk 能控制登录界面。RustDesk 会根据被注释的那一行判断登录界面是 Wayland，所以只改不删还是不行 | `/etc/gdm3/custom.conf` |
| 虚拟显示器配置、开机自动切换服务 | 没接显示器时也有桌面画面；接上显示器后自动改用真实显示器 | `/etc/X11/xorg.conf.d/99-autorustdesk-dummy.conf`、`autorustdesk-display.service` |
| `systemctl mask sleep.target suspend.target …` | 防止 B 在登录界面闲置约 20 分钟后休眠失联（可在设置中关闭） | systemd |
| ufw 放行直连网段访问 21118/tcp（仅在 ufw 已启用时） | 防火墙 | ufw |
| 直连网口临时加一个地址（仅在 B 为固定 IP 时，重启后消失） | 让 RustDesk 走直连网段 | `ip addr` |

撤销系统层面的修改（不卸载 RustDesk）：`sudo python3 /usr/local/lib/autorustdesk/ard_remote.py revert`，然后重启 B。

## 常见问题

**启动时报 `xcb-cursor0 or libxcb-cursor0 is needed`，然后"已放弃 (核心已转储)"**

图形界面库 Qt 从 6.5 版开始，在 X11 桌面下需要系统库 `libxcb-cursor0`，而 Ubuntu 默认没有安装。

- 立即解决：执行 `sudo apt install libxcb-cursor0`。
- 新版本的程序：打包时会把这个库打进包里；源码运行缺库时，会给出上面这条提示，不会再崩溃。

**找不到电脑 B / 发现 B 很慢**
- 确认网线两头的网口指示灯亮，B 已开机。
- B 的 DHCP 可能已经放弃重试。Ubuntu 的 NetworkManager 连续失败几次后，会**停 5 分钟**才再试；只有网线重新接上时才会立刻重试。所以 5 秒内还没收到 B 的地址请求时，程序会让 A 的网口断开再接上一次（"电子拔插网线"），B 一般几秒内就会来要地址。25 秒还不行会再做一次更长的断开。仍然找不到时，可以手动拔插网线试试。
- B 是固定 IP 时，程序会自动扫描常见网段。如果 B 的网口被禁用，需要在 B 上启用。

**发现了 B，但 SSH 连不上**：B 需要安装并启动 `openssh-server`。Ubuntu 桌面版默认没有安装，需要先在 B 上执行一次 `sudo apt install openssh-server`（离线时可用 U 盘拷贝 deb 安装）。

**提示"这个网口好像连着一个局域网"**：链路上发现了别的 DHCP 服务器或多台设备。为避免干扰别人的网络，程序不会启动 DHCP 服务。请确认网线是直接插在 B 上的。

**提示"离线包里没有 Ubuntu 22.04 的依赖"**：B 的系统版本不在离线包里。重新制作离线包，并勾选这个版本。

**B 是 Ubuntu 26.04**：Ubuntu 26.04 起 GNOME 不再提供 Xorg 会话，只有 Wayland，所以：
- RustDesk 无法控制登录界面，B 没接显示器时也无法使用；
- 程序仍然可以安装并配置 RustDesk（会先询问），登录界面和显示设置保持原样；
- 需要有人在 B 上登录桌面后才能远程，连接时 B 上可能要确认共享屏幕。

**连上了但黑屏 / 没有画面**
- 在「设置 → 电脑 B」里确认虚拟显示器设置为"自动"或"始终使用"，重新点「一键连接」。
- B 有 `/etc/X11/xorg.conf` 或使用 NVIDIA 专有驱动时，虚拟显示器可能不生效。这时最简单的办法是在 B 上插一个 HDMI 显卡欺骗器（假负载）。

**直连网段与本机网络冲突**：程序会自动改用其它网段，也可以在「设置 → 网络」里指定。

**异常退出后 A 的网卡没有恢复**：重新打开程序后点「恢复本机网络」。也可以用命令行：Ubuntu 执行 `sudo autorustdesk helper --cleanup`（AppImage 为 `sudo ./AutoRustDesk-<版本>-x86_64.AppImage helper --cleanup`）；Windows 以管理员身份打开命令提示符，在安装目录下执行 `AutoRustDesk-cli.exe helper --cleanup`；macOS 执行 `sudo /Applications/AutoRustDesk.app/Contents/MacOS/AutoRustDesk helper --cleanup`。

**Windows 提示"没有安装 Npcap"**：不装 Npcap 时，B 用 DHCP 或开着 IPv6 都能找到；只有 B 是固定 IP、不在直连网段、又关闭了 IPv6 时会找不到。安装 [Npcap](https://npcap.com) 后重新点「一键连接」即可。

**macOS 上找不到用 DHCP 的 B**：如果在「系统设置 → 网络 → 防火墙 → 选项」里开启了"阻止所有传入连接"，B 的地址请求会被挡住，请暂时关闭这个选项。普通的防火墙开启状态不影响，程序会临时放行自己。

## 命令行用法

```bash
python3 -m autorustdesk                       # 图形界面
python3 -m autorustdesk bundle build x.deb    # 制作离线包
python3 -m autorustdesk bundle info x.tar     # 查看离线包
python3 -m autorustdesk connect --user robot  # 命令行执行完整流程（--bundle x.tar 指定离线包，打包版默认用自带的）
python3 -m autorustdesk connect --quick       # 已配置过的设备直接连接
```

打包版中，`python3 -m autorustdesk` 换成程序本身：Ubuntu 为 `autorustdesk`（deb）或 `./AutoRustDesk-<版本>-x86_64.AppImage`，Windows 为安装目录下的 `AutoRustDesk-cli.exe`，macOS 为 `/Applications/AutoRustDesk.app/Contents/MacOS/AutoRustDesk`。

## 开发与测试

```bash
pip install -r requirements.txt pytest
python3 -m pytest                                  # 单元测试 + GUI 冒烟测试
sudo python3 -m pytest tests/integration           # 用网络命名空间模拟网线直连（需要 root、dhclient；装了 libpcap 时也测 libpcap 抓包）
sudo python3 -m tests.e2e.run_e2e --bundle x.tar --ubuntu 22.04   # 端到端：A ⇄ 虚拟网线 ⇄ Ubuntu 容器（20.04/22.04/24.04，需要 Docker）
sudo python3 tests/platform_smoke.py               # 在真实的 macOS / Windows（管理员）上测试网卡接口和网络助手
python packaging/build.py                          # 打包 Windows / macOS 版（在对应系统上运行）
```

每次推送代码，GitHub Actions 会：

1. 在 Linux、Windows、macOS（Apple 芯片）上运行测试：Linux 上以 root 跑网络集成测试；macOS 上用 feth 虚拟网卡对当网线，完整走一遍配置、抓包、DHCP、ARP 扫描；Windows 上创建环回网卡，测试 netsh 配置和恢复。
2. 下载 RustDesk 官方最新正式版，制作电脑 B 的离线部署包（Ubuntu 20.04 / 22.04 / 24.04）。
3. 打包三个系统的完整离线版：Windows x64 安装程序、Apple 芯片 Mac 的 dmg、Ubuntu x86_64 的 deb 和 AppImage。每个包都按用户的用法验证一遍再运行自检：Windows 安装程序静默安装后运行，dmg 挂载后从里面运行，deb 在 Ubuntu 20.04 里安装后运行，AppImage 直接运行。

在 Actions 页面手动运行"测试与打包"时，可以指定打包哪个 RustDesk 版本。

**发布新版本**：先修改 `autorustdesk/__init__.py` 里的版本号，然后任选一种方式：

- 在网页上：打开 Actions 页面的"测试与打包"，点「Run workflow」，在 `release_tag` 里填版本号（如 `v0.2.0`），点运行；
- 用命令行：推送 `v` 开头的标签（`git tag v0.2.0 && git push origin v0.2.0`）。

GitHub Actions 会打好全部安装包，创建一个 Release 草稿，附上各个安装包、单独的离线部署包和校验和（`SHA256SUMS.txt`）。标签必须和程序版本号一致，否则不会创建。在 Releases 页面检查无误后点「发布」即可（网页方式的标签在这时创建）。

端到端测试用 `tests/fakes/make_fake_rustdesk.py` 生成的"假 RustDesk"deb 制作离线包。它的依赖列表和安装脚本与官方包一致，程序本体换成了模拟命令行行为的脚本。测试会在不联网的 Ubuntu 20.04 容器里真实地走一遍离线安装。测试覆盖五种场景：B 用 DHCP、B 的 DHCP 已放弃重试（模拟 NetworkManager，只在网线接上时请求地址）、B 是固定 IP 且有流量、B 是固定 IP 且完全不发报文、程序不退出时拔线换插另一台设备；另外还验证快速连接和重复运行的幂等性。

代码结构：

```
autorustdesk/
  bundle/        离线包：格式、制作（纯 Python 解析 Ubuntu 软件源并计算依赖闭包）
  helper/        以管理员权限运行的网络助手：网卡配置、DHCP 服务、链路监听、ARP 扫描、IPv6 探测
                 （backend.py 为各系统的接口，os_linux / os_macos / os_windows 为具体实现）
  remote/        在电脑 B 上执行的脚本 ard_remote.py（Python 3.8 标准库）
  core/          流程编排、SSH、设备记录、设置、网卡列表、本机 RustDesk
  gui/           PySide6 图形界面
  cli.py         命令行模式
packaging/       打包：PyInstaller（Linux：build_linux.sh + linux_packages.py；Windows / macOS：build.py）、
                 Windows 安装程序（windows/AutoRustDesk.iss）、图标、下载 RustDesk 官方安装包（fetch_rustdesk.py）
tests/           单元测试、集成测试、端到端测试
```

## 路线图

- [x] 第一期：电脑 A = Ubuntu
- [x] 第二期：电脑 A = Windows（netsh 配置网卡、UAC 授权网络助手、Npcap 可选）——待真机验证
- [x] 第三期：电脑 A = macOS（ifconfig 临时地址、系统自带 libpcap、管理员密码框授权）——待真机验证
- [ ] B 没有 SSH 时的 U 盘引导包
- [ ] 通过自建 RustDesk 服务器跨网络远程

## 说明

RustDesk 是 RustDesk 团队的开源软件（AGPL-3.0）。本工具只分发和调用官方原版安装包，不修改 RustDesk 本身。
