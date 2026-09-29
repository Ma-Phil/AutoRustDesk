import socket
import struct

from autorustdesk.helper import dhcp as D

MAC_B = "52:54:00:12:34:56"
MAC_C = "52:54:00:aa:bb:cc"


def make_req(mtype, mac=MAC_B, xid=0x1234, requested=None, server_id=None, ciaddr="0.0.0.0",
             hostname=None):
    p = D.DhcpPacket()
    p.op = 1
    p.xid = xid
    p.ciaddr = ciaddr
    p.chaddr = D.mac_bytes(mac)
    p.options[D.OPT_MSG_TYPE] = bytes([mtype])
    if requested:
        p.options[D.OPT_REQUESTED_IP] = socket.inet_aton(requested)
    if server_id:
        p.options[D.OPT_SERVER_ID] = socket.inet_aton(server_id)
    if hostname:
        p.options[D.OPT_HOSTNAME] = hostname.encode()
    return p


def make_server(events=None, reservations=None):
    return D.DhcpServer(
        "eth0", "192.168.77.1", 24, "192.168.77.100", "192.168.77.110", 3600,
        reservations, (events.append if events is not None else None),
    )


def test_packet_roundtrip():
    p = make_req(D.DISCOVER, hostname="robot-01")
    data = p.build()
    assert len(data) >= 300
    q = D.DhcpPacket.parse(data)
    assert q.mac == MAC_B
    assert q.msg_type == D.DISCOVER
    assert q.opt_text(D.OPT_HOSTNAME) == "robot-01"
    assert q.xid == 0x1234


def test_full_exchange_no_router_no_dns():
    events = []
    s = make_server(events)
    offer, dest = s.handle(make_req(D.DISCOVER, hostname="robot-01"), now=1000)
    assert dest == "255.255.255.255"
    assert offer.msg_type == D.OFFER
    assert offer.yiaddr == "192.168.77.100"
    assert D.OPT_ROUTER not in offer.options
    assert D.OPT_DNS not in offer.options
    assert offer.opt_ip(D.OPT_SUBNET_MASK) == "255.255.255.0"
    assert offer.opt_ip(D.OPT_SERVER_ID) == "192.168.77.1"

    ack, dest = s.handle(
        make_req(D.REQUEST, requested="192.168.77.100", server_id="192.168.77.1", hostname="robot-01"),
        now=1001,
    )
    assert ack.msg_type == D.ACK and ack.yiaddr == "192.168.77.100"
    assert struct.unpack("!I", ack.options[D.OPT_LEASE_TIME])[0] == 3600
    leases = [e for e in events if e["event"] == "lease"]
    assert leases == [{"event": "lease", "mac": MAC_B, "ip": "192.168.77.100",
                       "hostname": "robot-01", "vendor": "", "prefix": 24}]


def test_renew_is_unicast_and_keeps_ip():
    s = make_server()
    s.handle(make_req(D.DISCOVER), now=0)
    s.handle(make_req(D.REQUEST, requested="192.168.77.100", server_id="192.168.77.1"), now=1)
    ack, dest = s.handle(make_req(D.REQUEST, ciaddr="192.168.77.100"), now=1800)
    assert ack.msg_type == D.ACK
    assert dest == "192.168.77.100"


def test_init_reboot_with_foreign_address_gets_nak():
    s = make_server()
    nak, dest = s.handle(make_req(D.REQUEST, requested="10.1.2.3"), now=0)
    assert nak.msg_type == D.NAK
    assert dest == "255.255.255.255"


def test_request_for_other_server_is_ignored():
    s = make_server()
    s.handle(make_req(D.DISCOVER), now=0)
    assert s.handle(make_req(D.REQUEST, requested="192.168.77.100", server_id="192.168.77.254"),
                    now=1) is None
    assert MAC_B not in s.leases


def test_reservation_and_distinct_devices():
    s = make_server(reservations={MAC_B: "192.168.77.105"})
    offer_b, _ = s.handle(make_req(D.DISCOVER, mac=MAC_B), now=0)
    assert offer_b.yiaddr == "192.168.77.105"
    offer_c, _ = s.handle(make_req(D.DISCOVER, mac=MAC_C, requested="192.168.77.105"), now=0)
    assert offer_c.yiaddr == "192.168.77.100"


def test_decline_marks_address_bad():
    s = make_server()
    s.handle(make_req(D.DISCOVER), now=0)
    s.handle(make_req(D.DECLINE, requested="192.168.77.100"), now=1)
    offer, _ = s.handle(make_req(D.DISCOVER), now=2)
    assert offer.yiaddr == "192.168.77.101"


def test_pool_exhaustion_returns_none():
    s = D.DhcpServer("eth0", "192.168.77.1", 24, "192.168.77.100", "192.168.77.100")
    assert s.handle(make_req(D.DISCOVER, mac=MAC_B), now=0)[0].yiaddr == "192.168.77.100"
    s.handle(make_req(D.REQUEST, mac=MAC_B, requested="192.168.77.100", server_id="192.168.77.1"), now=0)
    assert s.handle(make_req(D.DISCOVER, mac=MAC_C), now=1) is None


class FakeSock:
    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.sent = []

    def sendto(self, data, addr):
        if addr[0] in self.fail_on:
            raise OSError(51, "Network is unreachable")
        self.sent.append(addr)


def test_broadcast_falls_back_to_subnet_broadcast():
    """macOS 上绑定网卡的套接字发不出 255.255.255.255，自动改发本网段广播地址。"""
    s = make_server()
    s._sock = FakeSock(fail_on={"255.255.255.255"})
    s._send(b"x", "255.255.255.255")
    assert s._sock.sent == [("192.168.77.255", 68)]
    assert s.subnet_broadcast  # 之后直接用本网段广播
    s._send(b"x", "255.255.255.255")
    assert s._sock.sent[-1] == ("192.168.77.255", 68)
    s._send(b"x", "192.168.77.100")  # 单播不受影响
    assert s._sock.sent[-1] == ("192.168.77.100", 68)
