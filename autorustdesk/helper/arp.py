"""主动 ARP 扫描（Linux AF_PACKET）。

B 已经有地址、又不发任何报文时，被动监听听不到它；向候选地址逐个发 ARP 请求，
B 会回应，回应由 sniffer 捕获。对不在本机网段的地址用 ARP 探测（发送方 IP 为
0.0.0.0），Linux 同样会回应。
"""

import ipaddress
import socket
import struct
import time
from typing import Iterable, List

ETH_ARP = 0x0806

# B 为固定 IP 且不发报文、又禁用了 IPv6 时的最后手段：扫描常见的私有网段
COMMON_SUBNETS = [
    "192.168.0.0/24", "192.168.1.0/24", "192.168.2.0/24", "192.168.3.0/24",
    "192.168.10.0/24", "192.168.31.0/24", "192.168.50.0/24", "192.168.88.0/24",
    "192.168.100.0/24", "192.168.123.0/24", "192.168.137.0/24", "10.0.0.0/24",
    "10.0.1.0/24", "10.1.1.0/24", "10.10.10.0/24", "172.16.0.0/24",
]


def expand_targets(specs: Iterable[str], limit: int = 8192) -> List[str]:
    out: List[str] = []
    for spec in specs:
        if "/" in spec:
            net = ipaddress.ip_network(spec, strict=False)
            if net.version != 4:
                continue
            hosts = list(net.hosts()) if net.num_addresses > 2 else list(net)
            out.extend(str(h) for h in hosts)
        else:
            out.append(str(ipaddress.IPv4Address(spec)))
        if len(out) > limit:
            raise ValueError("扫描地址太多（最多 %d 个）" % limit)
    return out


def build_request(src_mac: bytes, src_ip: str, target_ip: str) -> bytes:
    eth = b"\xff" * 6 + src_mac + struct.pack("!H", ETH_ARP)
    arp = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
    arp += src_mac + socket.inet_aton(src_ip) + b"\x00" * 6 + socket.inet_aton(target_ip)
    return eth + arp


def arp_scan(iface: str, src_mac: str, own_cidrs: Iterable[str], targets: List[str],
             pps: int = 2000) -> int:
    """对 targets 逐个发 ARP 请求；目标在本机网段内时用本机地址做发送方，否则用 0.0.0.0。"""
    mac = bytes(int(x, 16) for x in src_mac.split(":"))
    own = [ipaddress.ip_interface(c) for c in own_cidrs]
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_ARP))
    try:
        s.bind((iface, ETH_ARP))
        interval = 1.0 / max(pps, 1)
        sent = 0
        for t in targets:
            ip = ipaddress.ip_address(t)
            sender = "0.0.0.0"
            for o in own:
                if ip in o.network and ip != o.ip:
                    sender = str(o.ip)
                    break
            s.send(build_request(mac, sender, t))
            sent += 1
            time.sleep(interval)
        return sent
    finally:
        s.close()
