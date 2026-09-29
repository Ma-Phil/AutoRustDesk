# AutoRustDesk 设计说明

## 1. 总体架构

```
┌────────────── 电脑 A（Ubuntu / Windows / macOS）────────────────┐          ┌──── 电脑 B（Ubuntu 20.04~24.04）──┐
│  图形界面 gui/  ─┐                                               │          │                                   │
│  命令行 cli.py  ─┼─► 流程 core/workflow.py ──SSH（paramiko）────┼─ 网线 ──►│ ard_remote.py（上传后 sudo 运行） │
│                  │      │  设备记录/设置 core/devices,settings  │          │  probe / install / configure /    │
│                  │      │                                        │          │  link-ip / display-switch / revert│
│                  │      └─ JSON 行（管道，或连回本机端口）       │          │                                   │
│                  │           ▼                                   │          │ RustDesk（IP 直连 :21118）◄──────┤
│                  │  网络助手 helper/（管理员 / root）            │          └───────────────────────────────────┘
│                  │   网卡配置 / DHCP / 监听 / ARP / IPv6 探测    │
│  本机 RustDesk 客户端 ◄── rustdesk --connect <IP> --password …   │
└──────────────────────────────────────────────────────────────────┘
```

- **界面与流程分离**：`Workflow` 通过 `Ui` 回调输出进度、询问用户；图形界面和命令行各自实现 `Ui`。
- **权限最小化**：界面以普通用户运行；只有网络操作放在一个以管理员权限运行的**网络助手**进程里。助手只用标准库，与界面之间用 JSON 行通信。连接断开（界面退出或崩溃）时，助手自动恢复网卡并退出。
  - Linux：pkexec 弹出系统授权框，通过 stdin/stdout 通信。
  - macOS：osascript（`do shell script … with administrator privileges`）弹出系统的管理员密码框；Windows：弹出 UAC 确认框。提权后的进程拿不到界面的管道，所以界面在 `127.0.0.1` 的随机端口监听，助手连回来（`helper/transport.py`）。令牌放在只有当前用户和管理员能读的临时文件里，双方各出一个随机数、用令牌算 HMAC 作答，互相验证身份，令牌本身不经过网络。
- **B 端只用 Python 3.8 标准库**：Ubuntu 20.04 自带 python3（netplan 依赖它），脚本每次通过 SFTP 上传，不需要预装任何东西。

## 2. 网络方案

### 2.1 A 端网卡配置（`helper/backend.py` 及 `os_linux.py`、`os_macos.py`、`os_windows.py`）

网络助手只通过 `Backend` 接口操作网卡，各系统的实现：

**Linux**（`linkconfig.py`）
- 网卡由 NetworkManager 管理时，新建临时连接 `AutoRustDesk-<网卡>`，配置为：
  - `ipv4.method=manual`，地址 `192.168.77.1/24`；
  - `never-default=yes`，不改默认路由；
  - `ipv6.method=link-local`；
  - `autoconnect=no`，不会在别的场合自动启用。

  结束时删除该连接，NetworkManager 会自动切回原来的连接。
- 其它情况下直接用 `ip addr add` 配置，结束时删除。
- ufw 已启用时，临时放行该网卡上的 UDP 67。

**macOS**
- 用 `ifconfig <网卡> inet 192.168.77.1 netmask 255.255.255.0 alias` 在网卡上追加地址，结束时 `-alias` 删除。不修改"系统设置"里的网络服务，不设网关，不影响 Wi-Fi。
- 开启了应用程序防火墙时，用 `socketfilterfw` 临时允许本程序接收传入连接（DHCP 请求）。

**Windows**
- 用 netsh 配置，命令里用网卡序号（网卡名可能是中文，序号没有编码问题）：
  - 网卡原来是"自动获得 IP 地址"：临时改为固定地址 `192.168.77.1`（`gateway=none`），结束时改回自动获得；
  - 原来就是固定地址：只追加一个地址，结束时删除。
- Windows 的地址配置会保存在系统里，所以**先写记录、再改配置**；助手意外退出（甚至断电）后，下次启动会按记录恢复。记录里同时保存网卡 GUID，USB 网卡换了插口、序号变了也能找回。
- 防火墙临时添加规则 `AutoRustDesk-DHCP`，放行 UDP 67。

**各系统共同**
- 直连网段与本机其它网络冲突时，自动改用备选网段（`192.168.78.0/24`、`10.77.77.0/24`、`172.31.77.0/24`）。
- 所有修改记录在状态文件里（Linux `/run/autorustdesk/links.json`，macOS `/var/run/autorustdesk/links.json`，Windows `%ProgramData%\AutoRustDesk\links.json`），异常退出后可以清理。
- 配置前先看网卡是否已经从别的 DHCP 服务器拿到了地址（Linux 看 NetworkManager 的租约；macOS 看 `ipconfig getpacket` 且地址仍在网卡上；Windows 看 DHCP 分配的地址），并且网关能 ping 通：是的话说明插的是局域网，不启动 DHCP。
- 助手监视网线状态：拔线换插另一台 B 时，直连地址可能丢失；网线重新接上后，助手会自动把直连配置补回来。

### 2.2 DHCP 服务（`helper/dhcp.py`）

- 只收发直连网卡上的报文：Linux 用 `SO_BINDTODEVICE`，macOS 用 `IP_BOUND_IF`，Windows 绑定在网卡地址上（Windows 上这样的套接字能收到该网卡上的广播）并设置 `IP_UNICAST_IF`。地址池 `.100–.199`。
- **不下发网关（option 3）和 DNS（option 6）**，B 的默认路由和原有网络不受影响。
- 按 MAC 固定分配地址（设备记录里的保留地址），避免同型号设备轮流使用同一个 IP。
- 请求的地址不属于本网段时回 NAK（B 带着别的网络的旧租约时，能立刻重新申请）。
- **防误伤**：先监听 3 秒。发现别的 DHCP 服务器或多台设备，就判断插的是局域网，不启动 DHCP。

### 2.3 发现 B（`helper/sniffer.py`、`helper/arp.py`、`helper/icmp6.py`）

收发原始以太网帧（`helper/packetio.py`）：Linux 用 AF_PACKET；macOS 用系统自带的 libpcap；Windows 用 Npcap 的 wpcap.dll。libpcap 通过 ctypes 调用，不需要编译。

几种办法同时进行，任何一个先找到就行：

| 办法 | 适用情况 |
|---|---|
| DHCP 分配记录 | B 的网口是 DHCP（Ubuntu 桌面版默认） |
| 被动监听：ARP、IPv4、IPv6、DHCP 报文的源地址 | B 有任何流量 |
| 向 `ff02::1` 发 ICMPv6 ping，得到 B 的 `fe80::` 地址 | B 开着 IPv6（Linux 默认开启），不管 IPv4 怎么配置 |
| 对直连网段做 ARP 扫描：前 30 秒每 3 秒一次，之后每 10 秒一次（254 个地址约 0.1 秒，发出后由监听收集回应，不逐个等待） | B 还拿着上次的地址，又不发报文 |
| 3 秒没有任何发现时，对常见私有网段做一次 ARP 探测（发送方 IP 0.0.0.0，Linux 会回应；约 4000 个地址，约 1 秒） | B 是固定 IP、不发报文，且禁用了 IPv6 |
| **电子拔插网线**：5 秒内 B 既没有地址、也没来请求地址时，让 A 的网口断开再接上 | B 的 DHCP 已放弃重试（见下） |

IPv6 探测和 ARP 扫描都是"发出即返回"，回应由链路监听收集。发现循环由事件驱动：一收到新信息就立即处理，没有新信息时最多等 0.3 秒。

**Windows 没装 Npcap 时**不能抓包，改为：DHCP 分配记录照常；每秒读取系统邻居表（`GetIpNetTable2`），B 回应 IPv6 探测时会先询问 A 的 MAC，A 的邻居表里就有了 B 的 `fe80::` 地址；直连网段的 ARP 扫描改用系统的 `SendARP`（32 个线程并发）。只有"B 是固定 IP、不在直连网段、又关闭了 IPv6"这一种情况找不到，界面会提示安装 Npcap。

**电子拔插网线**的原因：NetworkManager 1.22（Ubuntu 20.04）有以下行为，都已在源码中确认：

- 单次 DHCP 最多等 45 秒（`NM_DHCP_TIMEOUT_DEFAULT`）；
- 连续失败、自动重试用完后，**停 300 秒**才再试（`AUTOCONNECT_RESET_RETRIES_TIMER`）；
- 但网线重新接上（UNAVAILABLE → DISCONNECTED，原因为 carrier）时会重置重试次数并立刻重连（`nm-policy.c`："if cable was unplugged and plugged in again, we should try to reconnect"）；
- 正在进行中的连接对断线有 6 秒宽限期（`CARRIER_WAIT_TIME_MS`）。

所以如果 B 在 A 的 DHCP 服务就绪之前就插上了网线，它可能要等好几分钟才会再来要地址。电子拔插分两次：

1. 5 秒时先做一次"快速"的：Linux 上让网卡重新协商链路（`SIOCETHTOOL`/`ETHTOOL_NWAY_RST`，等同 `ethtool -r`），断开 2~3 秒，A 自己的 NetworkManager 连接不受影响；网卡不支持重新协商时，以及在 macOS（`ifconfig down/up`）和 Windows（停用再启用网卡）上，改为关闭网口 2 秒。
2. 25 秒仍没有发现 B，再做一次"长"的：关闭网口 7 秒，超过 6 秒的宽限期，B 正在进行中的 DHCP 也会重来。

Windows 停用网卡后，绑定在网卡地址上的 DHCP 套接字会失效，拔插结束后助手会重新打开它（已分配的地址保留）；抓包句柄出错时也会自动重新打开。

拔插期间，助手不上报断线事件；拔插结束后，助手会检查直连配置，必要时重新启用。

B 是固定 IP、不在直连网段时，A 在 B 的 /24 网段临时加一个地址（`.254`）去访问 B；也可以直接通过 IPv6 链路本地地址 SSH 登录。

### 2.4 RustDesk 走的地址

RustDesk 的 IP 直连使用 IPv4。B 的直连网口没有直连网段地址时（固定 IP 的情况），`ard_remote.py link-ip` 会给它**临时**加一个地址。这个地址按 MAC 分配，重启后消失，不修改 B 的网络配置。

## 3. 离线部署包（`bundle/`）

### 3.1 支持的 B 系统

| Ubuntu | 代号 | 支持程度 | 说明 |
|---|---|---|---|
| 20.04 | focal | 完整 | 最初的目标系统 |
| 22.04 | jammy | 完整 | `ubuntu-session` 仍提供 `xsessions/ubuntu-xorg.desktop` |
| 24.04 | noble | 完整 | 同上；依赖用 t64 包名，RustDesk 的备选依赖已覆盖 |
| 26.04 | resolute | 部分 | `ubuntu-session` 只剩 Wayland 会话（已在软件源的 Contents 中确认），见 4.1 |

### 3.2 格式

离线包是一个 tar 文件（格式 2），一个包里可以含多个 Ubuntu 版本：

```
autorustdesk-bundle/
    manifest.json        RustDesk 信息、install_packages、releases{代号: 版本号 + 包清单}
    pool/*.deb           RustDesk 本身和各版本的依赖，同名文件只存一份（Ubuntu 的 pool 文件名含版本号）
    repos/<代号>/        各版本本地 apt 源的索引：Packages、Packages.gz、Release
```

连接时，A 从离线包里取出 B 那个版本的**单版本包**（格式 1：`manifest.json` + `repo/`，其中 `Packages` 和对应的 `.deb` 放在一起），只上传这部分。B 端脚本只认识格式 1。第一版做的只含 20.04 的格式 1 离线包仍可直接使用。

### 3.3 制作

纯 Python 实现，不依赖 apt 或 Docker，Windows、macOS、Linux 上都能运行。对每个选中的版本分别执行：

1. 读取 RustDesk deb 的 control（ar + tar，支持 gz、xz、zst）；
2. 下载 `<代号>`、`<代号>-updates`、`<代号>-security` 的 `Packages.xz`，按 InRelease 里的 SHA256 校验；有 gpgv 时还校验签名；
3. 从 `Depends`、`Pre-Depends` 出发计算**完整依赖闭包**：
   - 支持备选依赖（`a | b`）、版本约束、虚拟包（Provides）；
   - 有多个提供者时，优先选 Priority 高的；
   - 版本比较算法与 dpkg 一致；
4. 下载每个 deb 并校验 SHA256，同时带上 `xserver-xorg-video-dummy`。

为什么带完整闭包（连 libc6 都带）：B 上 apt 只安装缺少的包、必要时升级，多带的包不会被装上。这样即使 B 是某个版本最早的发行版，一直没有更新过，也能装得上。

### 3.4 B 端安装（`ard_remote.py install`）

- 离线包的目标版本必须与 B 的版本代号一致，否则直接报错，提示重新制作。混用不同版本的依赖可能把系统库装乱。
- 用单独的 `sources.list`、`lists` 目录指向本地源，不碰 B 原有的 apt 配置。
- `apt-get install --no-install-recommends --no-remove`，绝不删除 B 上的包。
- 处理 dpkg 被中断、依赖损坏等情况。

## 4. B 端配置（`ard_remote.py configure`）

### 4.1 只有 Wayland 的系统（Ubuntu 26.04 起）

判断方法：版本代号在已知的"只有 Wayland"名单里，或者 `/usr/share/xsessions/` 下没有任何 Xorg 会话。满足任一条件时：

- 不修改 GDM 的 `WaylandEnable`（改了会导致无法登录）；
- 不启用虚拟显示器（它是给 Xorg 用的）；
- 不重启登录界面；
- 其余 RustDesk 设置照常进行。

没有图形会话时，RustDesk 的 IPC 不可用，固定密码设不上。这时会明确提示"先在 B 上接显示器并登录桌面"。A 端在检查环境这一步就会说明这些限制，并询问是否继续。

新版 RustDesk 源码里有一个 `rustdesk-unattended-wayland` 变体（通过 DRM 直接截屏），将来可能用来支持 26.04 的无人值守，目前没有验证。

### 4.2 配置步骤（能使用 Xorg 的系统）

1. **登录界面**：`/etc/gdm3/custom.conf` 的 `[daemon]` 中设置 `WaylandEnable=false`，并删除所有被注释的 `WaylandEnable` 行。RustDesk 源码里的 `is_login_wayland()` 用正则 `# *WaylandEnable *= *false` 判断，只要注释行还在，它就认为登录界面是 Wayland。
2. **虚拟显示器**：新版 RustDesk 已移除 `allow-linux-headless`，改由我们自己配置：
   - 模板 `/etc/autorustdesk/xorg-dummy.conf` 使用 dummy 驱动，Modeline 按 VESA CVT 计算，结果与 `cvt` 工具一致，默认 1920x1080；
   - `autorustdesk-display.service` 在显示管理器启动前运行：检测到 DRM 接口有显示器连接，就移除 `/etc/X11/xorg.conf.d/99-autorustdesk-dummy.conf`，否则启用它。
3. **防止休眠**：mask `sleep`、`suspend`、`hibernate`、`hybrid-sleep` 这几个 target。
4. **防火墙**：ufw 启用时，放行直连网段访问 21118/tcp。
5. **重启登录界面**：显示设置有变化时，如果 B 上**没有用户登录**就自动重启显示管理器，并等待 greeter 会话出现；有用户登录时先询问。
6. **RustDesk 设置**：先等 RustDesk 服务就绪（`--get-id` 能取到 ID），再用 `rustdesk --option` 设置以下选项，每项都回读校验：
   - `direct-server=Y`
   - `direct-access-port`
   - `verification-method=use-permanent-password`
   - `approve-mode=password`
   - `whitelist`（与已有白名单合并）

   然后用 `rustdesk --password` 设置固定密码，直到返回 `Done!` 为止。
7. **校验**：`/proc/net/tcp` 中 21118 端口处于监听状态。

所有修改都写入 `/etc/autorustdesk/state.json`，`revert` 子命令会据此撤销。

## 5. 设备记录与安全

- 设备记录（`~/.config/autorustdesk/devices.json`，权限 0600）按 MAC 保存：主机名、直连地址、SSH 账号、主机密钥、RustDesk 密码/版本/ID。
- **SSH 主机密钥按 MAC 核对**：不使用 known_hosts，因为不同设备可能先后使用同一个 IP。密钥变化时提示用户确认。
- RustDesk 密码默认每台设备随机生成（12 位字母数字），也可以设为统一密码。
- 密码不出现在 A 发往 B 的命令行里：通过 SFTP 写入 0600 的临时文件，用后立即删除。sudo 密码通过 stdin 传给 `sudo -S`。
- IP 直连端口默认对 B 的所有网卡开放，所以用 RustDesk 白名单限制为只允许直连网段连接。

## 6. 各系统的实现

| | Linux | Windows | macOS |
|---|---|---|---|
| 提权 | pkexec | UAC（`ShellExecuteEx` + `runas`） | osascript 管理员密码框 |
| 与界面通信 | 管道 | 连回本机端口 + 令牌互验 | 连回本机端口 + 令牌互验 |
| 网卡配置 | NetworkManager 临时连接 / ip | netsh（必要时临时改固定地址，结束后改回） | ifconfig alias |
| 网卡列表、网线状态 | /sys/class/net | iphlpapi `GetAdaptersAddresses` | ifconfig + networksetup |
| DHCP 套接字 | SO_BINDTODEVICE | 绑定网卡地址 + IP_UNICAST_IF | IP_BOUND_IF |
| 抓包、ARP | AF_PACKET | Npcap（可选；没有时用邻居表 + SendARP） | 系统自带 libpcap |
| IPv6 探测 | 原始套接字 | 原始套接字（自己算校验和） | 原始套接字 |
| 电子拔插 | 重新协商链路 / ip link | 停用再启用网卡 | ifconfig down/up |
| 防火墙 | ufw（启用时） | 添加入站规则 | socketfilterfw（启用时） |
| 打包 | PyInstaller → deb、AppImage | PyInstaller：AutoRustDesk.exe + AutoRustDesk-cli.exe → Inno Setup 安装程序 | PyInstaller：AutoRustDesk.app → dmg |

`helper/` 的命令协议与平台无关，`server.py` 只通过 `Backend` 接口操作网卡。

### 6.1 完整离线版

发布的安装包自带电脑 B 的离线部署包和电脑 A 用的 RustDesk 官方客户端，装好后 A 全程不用联网。

- **程序怎么找到自带的资源**（`core/builtin.py`）：在程序目录（PyInstaller 的 `sys._MEIPASS` 和可执行文件所在目录）下找 `bundle/*.tar` 和 `rustdesk/`。
  - 离线包：用户没选、或选过的文件已经不在时，用自带的；
  - RustDesk 客户端：优先用本机已安装的，找不到时才用自带的。Windows 为 `rustdesk/rustdesk-<版本>-x86_64.exe`（官方的免安装版，直接运行）；Linux 为官方 AppImage（加 `--appimage-extract-and-run`，不依赖 FUSE）；macOS 为安装盘里与本程序并列的 `RustDesk.app`（保留官方签名，用户一起拖进「应用程序」）。
- **各系统的安装包**：

| | 安装包 | 说明 |
|---|---|---|
| Windows | Inno Setup 安装程序 | 中文界面（Inno Setup 没带简体中文时用 `packaging/windows/ChineseSimplified.isl`）；默认装在当前用户目录，不需要管理员权限（也可以选择为所有用户安装）；离线包放在安装目录的 `bundle/` 下 |
| macOS | dmg（只做 Apple 芯片版；最低 macOS 版本跟着打包进去的 Qt 走，写进 Info.plist，目前为 13） | 离线包在签名前放进 `AutoRustDesk.app/Contents/Resources/bundle/`；dmg 里放本程序、官方的 `RustDesk.app` 和「应用程序」快捷方式 |
| Ubuntu | deb | 装到 `/opt/autorustdesk`，带应用菜单项、图标、`/usr/bin/autorustdesk`，以及 polkit 策略（授权框显示本程序的说明，授权在一段时间内有效） |
| Ubuntu | AppImage | 单个文件。以 root 运行网络助手时，pkexec 执行的是 AppImage 文件本身（`$APPIMAGE`）：AppImage 挂载的目录只有当前用户能访问，root 进不去 |

- **Ubuntu 版在 Ubuntu 20.04 的容器里打包**，打出来的程序在 20.04 及以后的版本上都能运行（glibc 向后兼容）。
- **Npcap 不能打包**：它的许可证不允许随其它软件分发，只能让用户自己装（不装也能用，见 2.3）。
- **CI**（`.github/workflows/build.yml`）：先用 `packaging/fetch_rustdesk.py` 下载 RustDesk 官方最新正式版（按 GitHub 给出的 SHA256 校验），制作一份 Ubuntu 20.04 / 22.04 / 24.04 的离线包，三个系统的打包任务共用这一份；每个安装包打好后按用户的用法验证一遍，运行自检（`selftest` 报告自带的离线包和客户端，找不到就算失败）：Windows 安装程序静默安装后运行装好的程序；dmg 挂载后从里面运行，并检查 RustDesk.app 的官方签名完好；deb 在 Ubuntu 20.04 里真实安装后运行；AppImage 直接运行。推送 `v*` 标签时，把全部安装包、单独的离线包和 `SHA256SUMS.txt` 放进 Release 草稿，人工确认后发布。

## 7. 测试

- **单元测试**：dpkg 版本比较、依赖解析、DHCP 报文与分配逻辑、链路报文解析、GDM 配置修改（用 RustDesk 的判断逻辑验证）、CVT Modeline（对照 `cvt` 输出）、设置与设备记录、界面冒烟测试。
- **macOS / Windows 的逻辑单元测试**：用真实系统的命令输出样例测解析；用假的命令执行器验证 ifconfig / netsh 的调用顺序、恢复和崩溃后清理；Windows 结构体的大小和字段偏移与 SDK 对照；在内存里构造 `IP_ADAPTER_ADDRESSES` 等结构测试解析。
- **集成测试**（`tests/integration`）：用网络命名空间和 veth 模拟网线直连。抓包分别用 AF_PACKET、libpcap（macOS/Windows 用的实现）和"不能抓包"（模拟 Windows 没装 Npcap）三种方式各跑一遍，覆盖以下场景：
  - 真实 dhclient 获取地址（验证不下发默认路由）；
  - 固定 IP 设备的被动发现；
  - 不发报文的固定 IP 设备的主动 ARP 扫描（本网段请求与常见网段探测）；
  - 外来 DHCP 服务器的防误伤；
  - 电子拔插（B 端能看到断开再接上，A 的配置保持不变）；
  - 异常退出后的清理；
  - 连回界面的通信方式（令牌错误时双方都拒绝）。
- **真实 Windows / macOS**（`tests/platform_smoke.py`，在 GitHub Actions 上以管理员运行）：
  - 两个系统：列网卡、读网线状态、以管理员启动助手并连回、在真实网卡上监听；
  - macOS：用 feth 虚拟网卡对当网线，完整走一遍配置地址、libpcap 抓包、ARP 扫描、模拟 B 的 DHCP 请求拿到地址、电子拔插、退出后恢复；
  - Windows：真实调用 iphlpapi（验证结构体布局）、SendARP；创建 KM-TEST 环回网卡，验证 netsh 配置固定地址、防火墙规则、DHCP 服务收包、停用再启用网卡、退出后恢复为原来的获取方式。
  然后打包，并运行打包后程序的自检（`selftest`：加载界面、SSH、网卡接口）。
- **端到端测试**（`tests/e2e`）：A 与一个 Ubuntu 容器（20.04 / 22.04 / 24.04，`--network none`，SSH + sudo + dhclient + systemd 桩）之间用 veth 相连，用同一个多版本离线包完整跑通离线安装和配置，覆盖以下场景：
  - DHCP、DHCP 已放弃重试（模拟 NetworkManager，只在网线接上时请求地址，验证电子拔插）、固定 IP、静默固定 IP 四种网络情况；
  - 程序不退出时拔线换插另一台设备；
  - 快速连接；
  - 重复运行的幂等性。
