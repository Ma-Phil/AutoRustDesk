"""向 IPv6 全节点组播地址 ff02::1 发 ping，找出链路上的设备。

Linux 网卡默认都有 fe80:: 链路本地地址，并且会回应组播 ping，
所以即使 B 是固定 IPv4、我们不知道它的网段，也能通过 IPv6 找到并 SSH 上去。
"""

import os
import socket
import struct
import sys
import time
from typing import Optional

ICMP6_ECHO_REQUEST = 128
ALL_NODES = "ff02::1"

# 各系统的套接字选项值（Python 通常自带这些常量，这里只作后备）
_OPTS = {"linux": (17, 18, 19), "darwin": (9, 10, 11), "win32": (9, 10, 11)}
_IF, _HOPS, _LOOP = _OPTS.get("linux" if sys.platform.startswith("linux") else sys.platform, (9, 10, 11))
IPV6_MULTICAST_IF = getattr(socket, "IPV6_MULTICAST_IF", _IF)
IPV6_MULTICAST_HOPS = getattr(socket, "IPV6_MULTICAST_HOPS", _HOPS)
IPV6_MULTICAST_LOOP = getattr(socket, "IPV6_MULTICAST_LOOP", _LOOP)


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def echo_request(ident: int, seq: int, src: Optional[str] = None, dst: str = ALL_NODES) -> bytes:
    """ICMPv6 回显请求。给出源地址时填好校验和。

    Linux 和 macOS 的内核总是自己计算 ICMPv6 校验和；Windows 不一定，所以自己算好，
    内核重算也不影响。
    """
    payload = b"AutoRustDesk"
    msg = struct.pack("!BBHHH", ICMP6_ECHO_REQUEST, 0, 0, ident, seq) + payload
    if not src:
        return msg
    pseudo = socket.inet_pton(socket.AF_INET6, src.split("%")[0])
    pseudo += socket.inet_pton(socket.AF_INET6, dst)
    pseudo += struct.pack("!I3xB", len(msg), socket.IPPROTO_ICMPV6)
    csum = _checksum(pseudo + msg)
    return msg[:2] + struct.pack("!H", csum) + msg[4:]


def probe_all_nodes(ifindex: int, count: int = 2, src_ll: Optional[str] = None) -> int:
    """向 ff02::1 发 ICMPv6 回显请求，不等回应。

    回应由链路监听（sniffer）收到并上报为 neighbor 事件，所以这里发完就返回，
    不会阻塞发现流程。src_ll 是本机在该网卡上的 fe80:: 地址（知道时绑定它，
    保证源地址和校验和一致）。返回发出的报文数。
    """
    s = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6)
    try:
        if src_ll:
            try:
                s.bind((src_ll.split("%")[0], 0, 0, ifindex))
            except OSError:
                src_ll = None  # 绑定失败就让内核选源地址，校验和也交给内核
        s.setsockopt(socket.IPPROTO_IPV6, IPV6_MULTICAST_IF, struct.pack("@I", ifindex))
        s.setsockopt(socket.IPPROTO_IPV6, IPV6_MULTICAST_HOPS, 1)
        s.setsockopt(socket.IPPROTO_IPV6, IPV6_MULTICAST_LOOP, 0)
        ident = os.getpid() & 0xFFFF
        for seq in range(1, count + 1):
            s.sendto(echo_request(ident, seq, src_ll), (ALL_NODES, 0, 0, ifindex))
            time.sleep(0.05)
        return count
    finally:
        s.close()
