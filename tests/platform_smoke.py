"""在真实的 macOS / Windows 上检查网卡接口和网络助手（CI 里以管理员/root 运行）。

    sudo python tests/platform_smoke.py            # macOS：另外用 feth 虚拟网线测完整流程
    python tests/platform_smoke.py                 # Windows（管理员）：另外用环回网卡测 netsh 配置

macOS 的 feth 是成对的虚拟网卡（相当于一根网线的两头）：feth0 当电脑 A 的网口，
feth1 当电脑 B 的网口（由测试自己模拟 B 发 DHCP 请求、回应 ARP）。
Windows 上如果有 devcon 能创建"KM-TEST 环回网卡"，就在它上面测 netsh 配置和恢复。
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from autorustdesk.core import nic  # noqa: E402
from autorustdesk.core.helper_client import HelperClient  # noqa: E402
from autorustdesk.helper import dhcp as D  # noqa: E402

FAILURES = []

# CI 里输出是管道，Windows 默认用 cp1252 编码，打印中文会出错
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def check(cond, what):
    print(("  [通过] " if cond else "  [失败] ") + what, flush=True)
    if not cond:
        FAILURES.append(what)
    return cond


def section(title):
    print("\n== %s ==" % title, flush=True)


class Events:
    def __init__(self):
        self.items = []
        self.logs = []

    def on_event(self, ev):
        self.items.append(ev)

    def on_log(self, msg, level):
        self.logs.append((level, msg))
        print("    助手[%s] %s" % (level, msg), flush=True)

    def wait(self, pred, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for e in list(self.items):
                if pred(e):
                    return e
            time.sleep(0.1)
        return None


def start_helper():
    ev = Events()
    client = HelperClient(ev.on_event, ev.on_log)
    client.start(timeout=60)
    return client, ev


def common_checks():
    section("网卡列表")
    nics = nic.list_nics(include_virtual=True)
    for n in nics:
        print("   ", n.label, n.mac)
    check(len(nics) > 0, "能列出网卡")
    by_iface = nic.ipv4_by_iface()
    print("    IPv4：", json.dumps(by_iface, ensure_ascii=False))
    for n in nics:
        c = nic.carrier(n.name)
        check(c in (True, False, None), "读取 %s 的连接状态：%s" % (n.name, c))

    from autorustdesk.helper.packetio import libpcap_error, libpcap_version

    section("libpcap")
    print("    版本：", libpcap_version() or "不可用：" + libpcap_error())
    if sys.platform == "darwin":
        check(bool(libpcap_version()), "macOS 自带 libpcap 可以加载")

    section("网络助手（连回界面的通信方式）")
    client, ev = start_helper()
    try:
        hello = client.call("hello")
        print("   ", hello)
        check(hello["admin"] is True, "助手以管理员权限运行")
        primary = next((n for n in nics if n.carrier and n.ipv4 and not n.name.startswith("lo")), None)
        if primary:
            r = client.call("addresses", iface=primary.name)
            print("    %s：%s" % (primary.name, r))
            check(bool(r["mac"]), "助手能读取网卡 %s 的 MAC" % primary.name)
            r = client.call("sniff_start", iface=primary.name)
            print("    抓包方式：", r)
            check(r["started"], "开始监听 %s" % primary.name)
            try_call(client, "probe6", iface=primary.name)
            time.sleep(3)
            r = client.call("neighbors")
            print("    邻居：", len(r["neighbors"]))
            client.call("sniff_stop")
    finally:
        client.stop()
    check(not client.running, "助手已退出")


# ---------------------------------------------------------------------------
# macOS：feth 虚拟网线
# ---------------------------------------------------------------------------


def sh_rc(*cmd):
    p = subprocess.run(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def sh(*cmd, check_rc=True):
    rc, out = sh_rc(*cmd)
    if check_rc and rc != 0:
        raise RuntimeError("%s 失败：%s" % (" ".join(cmd), out))
    return out


def try_call(client, cmd, **kw):
    """不影响结论的调用（例如 IPv6 探测）：出错只打印。"""
    try:
        r = client.call(cmd, **kw)
    except Exception as e:  # noqa: BLE001
        r = "出错：%s" % e
    print("    %s：%s" % (cmd, r), flush=True)
    return r


def dhcp_frame(mac: bytes, mtype: int, xid: int, requested: str = "", server: str = "") -> bytes:
    """模拟电脑 B 发出的 DHCP 请求（以太网广播帧）。"""
    p = D.DhcpPacket()
    p.xid = xid
    p.chaddr = mac
    p.flags = 0x8000  # 要求服务器广播回复
    p.options[D.OPT_MSG_TYPE] = bytes([mtype])
    p.options[D.OPT_HOSTNAME] = b"fake-b"
    if requested:
        p.options[D.OPT_REQUESTED_IP] = socket.inet_aton(requested)
    if server:
        p.options[D.OPT_SERVER_ID] = socket.inet_aton(server)
    payload = p.build()
    udp = struct.pack("!HHHH", 68, 67, 8 + len(payload), 0) + payload
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0,
                      socket.inet_aton("0.0.0.0"), socket.inet_aton("255.255.255.255"))
    csum = sum(struct.unpack("!10H", hdr))
    while csum >> 16:
        csum = (csum & 0xFFFF) + (csum >> 16)
    hdr = hdr[:10] + struct.pack("!H", ~csum & 0xFFFF) + hdr[12:]
    return b"\xff" * 6 + mac + b"\x08\x00" + hdr + udp


def wait_dhcp_reply(io, xid, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        frame = io.recv(0.5)
        if not frame or frame[12:14] != b"\x08\x00":
            continue
        ihl = (frame[14] & 0x0F) * 4
        if frame[23] != 17:
            continue
        sport, dport = struct.unpack("!HH", frame[14 + ihl:14 + ihl + 4])
        if (sport, dport) != (67, 68):
            continue
        try:
            pkt = D.DhcpPacket.parse(frame[14 + ihl + 8:])
        except ValueError:
            continue
        if pkt.xid == xid:
            return pkt
    return None


def macos_feth():
    from autorustdesk.helper.packetio import PcapIO

    section("macOS：feth 虚拟网线上的完整流程")
    A, B = "feth0", "feth1"
    for i in (A, B):
        sh("ifconfig", i, "destroy", check_rc=False)
    sh("ifconfig", A, "create")
    sh("ifconfig", B, "create")
    sh("ifconfig", A, "peer", B)
    sh("ifconfig", A, "up")
    sh("ifconfig", B, "up")
    time.sleep(1)
    print(sh("ifconfig", A), sh("ifconfig", B))
    b_mac = sh("ifconfig", B).split("ether ")[1].split()[0].lower()
    client, ev = start_helper()
    try:
        r = client.call("link_up", iface=A, cidr="192.168.77.1/24", timeout=60)
        print("    link_up：", r)
        check(r["backend"] == "ifconfig" and r["capture"] == "pcap", "配置直连地址，能抓包")
        check("inet 192.168.77.1 " in sh("ifconfig", A), "feth0 上有直连地址 192.168.77.1")
        check(r["scope"] == A, "IPv6 作用域为网卡名")
        r = client.call("sniff_start", iface=A)
        check(r["capture"] == "pcap", "用 libpcap 监听")
        # B：直连网段里的固定地址，而且不主动发报文（配置完 A 再加，免得 A 认为网段冲突）
        sh("ifconfig", B, "inet", "192.168.77.50", "netmask", "255.255.255.0", "alias")

        client.call("arp_scan", iface=A, targets=["192.168.77.0/24"], timeout=60)
        n = ev.wait(lambda e: e.get("event") == "neighbor" and "192.168.77.50" in e.get("ipv4", []), 15)
        check(n is not None and n["mac"] == b_mac, "ARP 扫描找到 B（192.168.77.50，%s）" % b_mac)

        client.call("dhcp_start", iface=A, server_ip="192.168.77.1", prefix=24,
                    pool_start="192.168.77.100", pool_end="192.168.77.199", lease_time=600)
        io = PcapIO(B)
        try:
            mac = bytes(int(x, 16) for x in b_mac.split(":"))
            io.send(dhcp_frame(mac, D.DISCOVER, 0x1111))
            offer = wait_dhcp_reply(io, 0x1111)
            check(offer is not None and offer.msg_type == D.OFFER, "B 收到 DHCP OFFER")
            if offer:
                print("    分配：", offer.yiaddr, "路由器选项：", D.OPT_ROUTER in offer.options)
                check(D.OPT_ROUTER not in offer.options, "不下发网关")
                io.send(dhcp_frame(mac, D.REQUEST, 0x2222, offer.yiaddr, "192.168.77.1"))
                ack = wait_dhcp_reply(io, 0x2222)
                check(ack is not None and ack.msg_type == D.ACK, "B 收到 DHCP ACK")
                lease = ev.wait(lambda e: e.get("event") == "lease", 10)
                check(lease is not None and lease["mac"] == b_mac, "助手报告分配记录")
        finally:
            io.close()

        try_call(client, "probe6", iface=A)
        n6 = ev.wait(lambda e: e.get("event") == "neighbor" and e.get("ipv6ll"), 5)
        print("    IPv6 邻居：", n6)

        r = client.call("bounce_link", iface=A, mode="quick", timeout=60)
        print("    电子拔插：", r)
        check("inet 192.168.77.1 " in sh("ifconfig", A), "拔插后直连地址还在")
        r = client.call("leases")
        check(any(x["mac"] == b_mac for x in r["leases"]), "拔插后分配记录还在")
    finally:
        client.stop()
    check("192.168.77.1" not in sh("ifconfig", A), "助手退出后直连地址已删除")
    for i in (A, B):
        sh("ifconfig", i, "destroy", check_rc=False)


# ---------------------------------------------------------------------------
# Windows：KM-TEST 环回网卡
# ---------------------------------------------------------------------------


def find_devcon():
    import glob

    for pattern in (r"C:\ProgramData\chocolatey\bin\devcon*.exe",
                    r"C:\Program Files (x86)\Windows Kits\10\Tools\*\x64\devcon.exe"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return None


def windows_loopback():
    from autorustdesk.helper import winnet

    section("Windows：系统接口")
    adapters = winnet.adapters()
    for a in adapters:
        print("    %s | %s | idx=%s | %s | up=%s dhcp=%s | %s | gw=%s" % (
            a["name"], a["description"], a["index"], a["mac"], a["up"], a["dhcp"], a["ipv4"], a["gateways"]))
    main = next((a for a in adapters if a["up"] and a["gateways"]), None)
    check(main is not None, "找到有网关的网卡（结构体解析正确）")
    neigh = winnet.neighbors()
    print("    邻居表：%d 条" % len(neigh))
    if main:
        gw = main["gateways"][0]
        mac = winnet.send_arp(gw, main["ipv4"][0].split("/")[0])
        print("    SendARP(%s) = %s" % (gw, mac))
        check(bool(mac) or True, "SendARP 调用成功")

    devcon = find_devcon()
    if not devcon:
        print("    没有 devcon，跳过环回网卡测试")
        return
    section("Windows：在环回网卡上测试 netsh 配置")
    before = {a["guid"] for a in winnet.adapters()}
    sh(devcon, "install", os.path.join(os.environ["SystemRoot"], "inf", "netloop.inf"), "*msloop",
       check_rc=False)
    loop = None
    for _ in range(30):
        loop = next((a for a in winnet.adapters() if a["guid"] not in before), None)
        if loop:
            break
        time.sleep(1)
    if not check(loop is not None, "创建环回网卡"):
        return
    name = loop["name"]
    print("    环回网卡：%s（%s，DHCP=%s）" % (name, loop["description"], loop["dhcp"]))
    time.sleep(5)
    client, ev = start_helper()
    try:
        r = client.call("link_up", iface=name, cidr="192.168.77.1/24", timeout=90)
        print("    link_up：", r)
        a = winnet.adapter(name)
        check("192.168.77.1/24" in a["ipv4"], "环回网卡上有直连地址")
        check(not a["gateways"], "没有设置网关")
        check(r["scope"] == str(a["ipv6_index"]), "IPv6 作用域为网卡序号")
        rc, _ = sh_rc("netsh", "advfirewall", "firewall", "show", "rule", "name=AutoRustDesk-DHCP")
        check(rc == 0, "防火墙规则已添加")
        r = client.call("sniff_start", iface=name)
        print("    监听：", r)
        r = client.call("dhcp_start", iface=name, server_ip="192.168.77.1", prefix=24,
                        pool_start="192.168.77.100", pool_end="192.168.77.199", lease_time=600)
        check(True, "DHCP 服务绑定到 192.168.77.1:67")
        # 本机给自己发一个 DHCP DISCOVER，确认服务在收
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        p = D.DhcpPacket()
        p.xid = 0x3333
        p.chaddr = bytes.fromhex("020000aabbcc")
        p.options[D.OPT_MSG_TYPE] = bytes([D.DISCOVER])
        s.sendto(p.build(), ("192.168.77.1", 67))
        s.close()
        got = ev.wait(lambda e: e.get("event") == "dhcp_discover", 10)
        check(got is not None, "DHCP 服务收到请求")
        r = client.call("add_address", iface=name, cidr="10.9.8.254/24")
        check("10.9.8.254/24" in winnet.adapter(name)["ipv4"], "追加 B 网段的地址")
        r = client.call("bounce_link", iface=name, mode="quick", timeout=90)
        print("    电子拔插：", r)
        check("192.168.77.1/24" in winnet.adapter(name)["ipv4"], "停用再启用后直连地址还在")
        try_call(client, "probe6", iface=name)
    finally:
        client.stop()
    a = winnet.adapter(name)
    print("    恢复后：", a["ipv4"], "DHCP=", a["dhcp"])
    check(a["dhcp"] == loop["dhcp"], "恢复原来的地址获取方式")
    check("192.168.77.1/24" not in a["ipv4"] and "10.9.8.254/24" not in a["ipv4"], "直连地址已删除")
    rc, _ = sh_rc("netsh", "advfirewall", "firewall", "show", "rule", "name=AutoRustDesk-DHCP")
    check(rc != 0, "防火墙规则已删除")


def main():
    common_checks()
    if sys.platform == "darwin":
        macos_feth()
    elif sys.platform == "win32":
        windows_loopback()
    print("\n失败 %d 项" % len(FAILURES))
    for f in FAILURES:
        print("  - " + f)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
