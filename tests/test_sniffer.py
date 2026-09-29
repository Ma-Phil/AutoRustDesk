import socket
import struct

from autorustdesk.helper import dhcp as D
from autorustdesk.helper.sniffer import NeighborTable, parse_frame

MAC_A = bytes.fromhex("020000000001")
MAC_B = bytes.fromhex("525400123456")
BCAST = b"\xff" * 6


def eth(src, dst, etype, body):
    return dst + src + struct.pack("!H", etype) + body


def arp_request(src_mac, spa, tpa):
    body = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1) + src_mac + socket.inet_aton(spa)
    body += b"\x00" * 6 + socket.inet_aton(tpa)
    return eth(src_mac, BCAST, 0x0806, body)


def ipv4_udp(src_mac, src_ip, dst_ip, sport, dport, payload):
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0,
                     socket.inet_aton(src_ip), socket.inet_aton(dst_ip))
    return eth(src_mac, BCAST, 0x0800, ip + udp)


def ipv6_frame(src_mac, src_ip):
    hdr = struct.pack("!IHBB", 0x60000000, 0, 58, 255)
    hdr += socket.inet_pton(socket.AF_INET6, src_ip) + socket.inet_pton(socket.AF_INET6, "ff02::1")
    return eth(src_mac, b"\x33\x33\x00\x00\x00\x01", 0x86DD, hdr)


def test_parse_arp_gives_static_ipv4():
    info = parse_frame(arp_request(MAC_B, "10.9.8.7", "10.9.8.1"))
    assert info["mac"] == "52:54:00:12:34:56"
    assert info["ipv4"] == "10.9.8.7"


def test_parse_arp_probe_ignores_zero_sender():
    info = parse_frame(arp_request(MAC_B, "0.0.0.0", "10.9.8.7"))
    assert "ipv4" not in info


def test_parse_dhcp_discover_hostname():
    p = D.DhcpPacket()
    p.chaddr = MAC_B
    p.options[D.OPT_MSG_TYPE] = bytes([D.DISCOVER])
    p.options[D.OPT_HOSTNAME] = b"robot-07"
    info = parse_frame(ipv4_udp(MAC_B, "0.0.0.0", "255.255.255.255", 68, 67, p.build()))
    assert info["dhcp_client"] is True
    assert info["hostname"] == "robot-07"
    assert "ipv4" not in info


def test_parse_ipv6_link_local():
    info = parse_frame(ipv6_frame(MAC_B, "fe80::5054:ff:fe12:3456"))
    assert info["ipv6ll"] == "fe80::5054:ff:fe12:3456"


def test_neighbor_table_events_and_foreign_dhcp():
    events = []
    t = NeighborTable(["02:00:00:00:00:01"], {"192.168.77.1"}, events.append)
    t.feed(parse_frame(arp_request(MAC_A, "192.168.77.1", "192.168.77.100")))  # 自己发的
    assert events == []
    t.feed(parse_frame(ipv6_frame(MAC_B, "fe80::5054:ff:fe12:3456")))
    t.feed(parse_frame(arp_request(MAC_B, "10.9.8.7", "10.9.8.1")))
    t.feed(parse_frame(arp_request(MAC_B, "10.9.8.7", "10.9.8.1")))  # 重复信息不再触发
    neigh = [e for e in events if e["event"] == "neighbor"]
    assert len(neigh) == 2
    assert neigh[-1]["ipv4"] == ["10.9.8.7"]
    assert neigh[-1]["ipv6ll"] == ["fe80::5054:ff:fe12:3456"]
    t.feed(parse_frame(ipv4_udp(MAC_B, "10.9.8.1", "255.255.255.255", 67, 68, b"x" * 10)))
    assert any(e["event"] == "foreign_dhcp" for e in events)
