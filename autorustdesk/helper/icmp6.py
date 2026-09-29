"""向 IPv6 全节点组播地址 ff02::1 发 ping，找出链路上的设备。

Linux 网卡默认都有 fe80:: 链路本地地址，并且会回应组播 ping，
所以即使 B 是固定 IPv4、我们不知道它的网段，也能通过 IPv6 找到并 SSH 上去。
"""

import os
import select
import socket
import struct
import time
from typing import List, Set

ICMP6_ECHO_REQUEST = 128
ICMP6_ECHO_REPLY = 129


def probe_all_nodes(iface: str, timeout: float = 2.0, own: Set[str] = frozenset()) -> List[str]:
    """返回回应了的链路本地地址（不含 %网卡 后缀，不含本机地址）。"""
    ifindex = socket.if_nametoindex(iface)
    s = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6)
    try:
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, struct.pack("@I", ifindex))
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 1)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_LOOP, 0)
        ident = os.getpid() & 0xFFFF
        found: List[str] = []
        for seq in (1, 2):
            # 校验和由内核填写（ICMPv6 原始套接字总是开启 IPV6_CHECKSUM）
            pkt = struct.pack("!BBHHH", ICMP6_ECHO_REQUEST, 0, 0, ident, seq) + b"AutoRustDesk"
            s.sendto(pkt, ("ff02::1", 0, 0, ifindex))
            deadline = time.time() + timeout / 2
            while True:
                left = deadline - time.time()
                if left <= 0:
                    break
                r, _, _ = select.select([s], [], [], left)
                if not r:
                    break
                data, addr = s.recvfrom(1500)
                if len(data) < 8 or data[0] != ICMP6_ECHO_REPLY:
                    continue
                if struct.unpack("!H", data[4:6])[0] != ident:
                    continue
                ip = addr[0].split("%", 1)[0]
                if ip not in own and ip not in found:
                    found.append(ip)
        return found
    finally:
        s.close()
