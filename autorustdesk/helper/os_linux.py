"""电脑 A 为 Linux（Ubuntu）时的网卡操作：配置见 linkconfig.py，抓包用 AF_PACKET。"""

import json
import os
import socket
from typing import Dict, List, Optional

from . import linkconfig as L
from .backend import Backend, Link, LogFn
from .packetio import AfPacketIO, PacketIO, PacketIOError, PcapIO


class LinuxBackend(Backend):
    name = "linux"

    def is_admin(self) -> bool:
        return os.geteuid() == 0

    def iface_exists(self, iface: str) -> bool:
        return L.iface_exists(iface)

    def iface_mac(self, iface: str) -> str:
        return L.iface_mac(iface)

    def iface_addresses(self, iface: str) -> Dict[str, List[str]]:
        return L.iface_addresses(iface)

    def carrier(self, iface: str) -> Optional[bool]:
        raw = L.read_sys(iface, "carrier")
        return None if raw == "" else raw == "1"

    def link(self, iface: str, log: LogFn) -> Link:
        return L.LinkConfigurator(iface, log)

    def cleanup_all(self, log: LogFn) -> List[str]:
        return L.cleanup_all(log)

    def packet_io(self, iface: str, send_only: bool = False) -> PacketIO:
        # 测试用：pcap 走 libpcap 实现（macOS/Windows 用的那套）；none 模拟 Windows 没装 Npcap
        mode = os.environ.get("AUTORUSTDESK_PACKETIO", "")
        if mode == "pcap":
            return PcapIO(iface, send_only)
        if mode == "none":
            raise PacketIOError("已禁用抓包")
        return AfPacketIO(iface, send_only)

    def neighbors(self, iface: str) -> List[Dict]:
        rc, out = L.run(["ip", "-j", "neigh", "show", "dev", iface])
        try:
            data = json.loads(out) if rc == 0 else []
        except ValueError:
            return []
        return [{"mac": e["lladdr"].lower(), "ip": e["dst"]} for e in data
                if e.get("lladdr") and not set(e.get("state", [])) & {"FAILED", "INCOMPLETE"}]

    def arp_resolve(self, iface: str, target: str, sender: str) -> Optional[str]:
        L.run(["ping", "-c", "1", "-W", "1", "-I", iface, target], timeout=5)
        for e in self.neighbors(iface):
            if e["ip"] == target:
                return e["mac"]
        return None

    def dhcp_socket(self, iface: str, server_ip: str) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # 只收发这块网卡上的 DHCP 报文，不影响 Wi-Fi 等其它网络
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode() + b"\x00")
            s.bind(("0.0.0.0", 67))
        except OSError:
            s.close()
            raise
        return s
