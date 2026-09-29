"""按操作系统选用网卡操作的实现。

网络助手（server.py）只通过这里的接口操作网卡，具体做法见：
- os_linux.py：NetworkManager / ip 命令，AF_PACKET 抓包
- os_macos.py：ifconfig，系统自带 libpcap 抓包
- os_windows.py：netsh + iphlpapi，Npcap 抓包（没装 Npcap 时读系统邻居表）
"""

import socket
import sys
from typing import Callable, Dict, List, Optional

from .packetio import PacketIO, PacketIOError

LogFn = Callable[[str], None]


class Link:
    """直连网卡的配置。up() 之后 primary 是本机在直连网段的地址（CIDR）。"""

    primary = ""

    def up(self, cidr: str) -> Dict:
        raise NotImplementedError

    def add_address(self, cidr: str) -> bool:
        raise NotImplementedError

    def ensure(self) -> bool:
        """网线拔插后把直连配置补回来，返回是否重新配置了。"""
        return False

    def bounce(self, mode: str = "quick") -> str:
        """电子"拔插网线"，返回实际采用的方式。"""
        raise NotImplementedError

    def down(self) -> None:
        raise NotImplementedError


class Backend:
    name = ""
    # 网卡被停用再启用后，DHCP 套接字是否需要重新绑定（Windows 绑定在网卡地址上）
    dhcp_rebind_after_bounce = False
    # DHCP 广播回复发往本网段广播地址而不是 255.255.255.255（macOS）
    dhcp_subnet_broadcast = False

    def is_admin(self) -> bool:
        raise NotImplementedError

    def iface_exists(self, iface: str) -> bool:
        raise NotImplementedError

    def iface_mac(self, iface: str) -> str:
        raise NotImplementedError

    def iface_addresses(self, iface: str) -> Dict[str, List[str]]:
        """{"ipv4": ["a.b.c.d/p", ...], "ipv6ll": ["fe80::...", ...], "ipv6": [...]}"""
        raise NotImplementedError

    def ifindex(self, iface: str) -> int:
        return socket.if_nametoindex(iface)

    def scope_id(self, iface: str) -> str:
        """访问 fe80:: 地址时 % 后面写的内容（Linux/macOS 是网卡名，Windows 是网卡序号）。"""
        return iface

    def carrier(self, iface: str) -> Optional[bool]:
        raise NotImplementedError

    def link(self, iface: str, log: LogFn) -> Link:
        raise NotImplementedError

    def cleanup_all(self, log: LogFn) -> List[str]:
        raise NotImplementedError

    def packet_io(self, iface: str, send_only: bool = False) -> PacketIO:
        raise PacketIOError("本系统不支持抓包")

    def dhcp_socket(self, iface: str, server_ip: str) -> socket.socket:
        raise NotImplementedError

    def neighbors(self, iface: str) -> List[Dict]:
        """系统邻居表中该网卡上的条目：[{"mac": ..., "ip": ...}]（不能抓包时使用）。"""
        return []

    def arp_resolve(self, iface: str, target: str, sender: str) -> Optional[str]:
        """不能抓包时用系统接口解析一个本网段地址的 MAC。"""
        return None


def current() -> Backend:
    if sys.platform.startswith("linux"):
        from .os_linux import LinuxBackend

        return LinuxBackend()
    if sys.platform == "darwin":
        from .os_macos import MacBackend

        return MacBackend()
    if sys.platform == "win32":
        from .os_windows import WindowsBackend

        return WindowsBackend()
    raise RuntimeError("不支持的操作系统：%s" % sys.platform)
