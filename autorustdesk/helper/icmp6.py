"""向 IPv6 全节点组播地址 ff02::1 发 ping，找出链路上的设备。

Linux 网卡默认都有 fe80:: 链路本地地址，并且会回应组播 ping，
所以即使 B 是固定 IPv4、我们不知道它的网段，也能通过 IPv6 找到并 SSH 上去。
"""

import os
import socket
import struct
import time

ICMP6_ECHO_REQUEST = 128


def probe_all_nodes(iface: str, count: int = 2) -> int:
    """向 ff02::1 发 ICMPv6 回显请求，不等回应。

    回应由链路监听（sniffer）收到并上报为 neighbor 事件，所以这里发完就返回，
    不会阻塞发现流程。返回发出的报文数。
    """
    ifindex = socket.if_nametoindex(iface)
    s = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6)
    try:
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, struct.pack("@I", ifindex))
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 1)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_LOOP, 0)
        ident = os.getpid() & 0xFFFF
        for seq in range(1, count + 1):
            # 校验和由内核填写（ICMPv6 原始套接字总是开启 IPV6_CHECKSUM）
            pkt = struct.pack("!BBHHH", ICMP6_ECHO_REQUEST, 0, 0, ident, seq) + b"AutoRustDesk"
            s.sendto(pkt, ("ff02::1", 0, 0, ifindex))
            time.sleep(0.05)
        return count
    finally:
        s.close()
