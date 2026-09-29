"""macOS 网卡操作：解析命令输出、按正确顺序调用 ifconfig（用假的命令执行器，在任何系统上都能测）。"""

import ipaddress
import json

from autorustdesk.helper import os_macos as M

IFCONFIG_A = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
	options=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
	inet 127.0.0.1 netmask 0xff000000
	inet6 ::1 prefixlen 128
	inet6 fe80::1%lo0 prefixlen 64 scopeid 0x1
	nd6 options=201<PERFORMNUD,DAD>
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	options=6460<TSO4,TSO6,CHANNEL_IO,PARTIAL_CSUM,ZEROINVERT_CSUM>
	ether 3c:22:fb:12:34:56
	inet6 fe80::1c8b:2f4e:1234:5678%en0 prefixlen 64 secured scopeid 0xb
	inet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255
	nd6 options=201<PERFORMNUD,DAD>
	media: autoselect
	status: active
en5: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	options=6464<VLAN_MTU,TSO4,TSO6,CHANNEL_IO,PARTIAL_CSUM,ZEROINVERT_CSUM>
	ether 00:E0:4C:68:01:02
	inet6 fe80::10f3:5b1f:9d2e:abcd%en5 prefixlen 64 secured scopeid 0x13
	inet 169.254.33.44 netmask 0xffff0000 broadcast 169.254.255.255
	nd6 options=201<PERFORMNUD,DAD>
	media: autoselect (1000baseT <full-duplex>)
	status: active
bridge0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	options=63<RXCSUM,TXCSUM,TSO4,TSO6>
	ether 36:a1:0b:22:33:44
	Configuration:
		id 0:0:0:0:0:0 priority 0 hellotime 0 fwddelay 0
	member: en1 flags=3<LEARNING,DISCOVER>
	nd6 options=201<PERFORMNUD,DAD>
	media: <unknown type>
	status: inactive
en1: flags=8963<UP,BROADCAST,SMART,RUNNING,PROMISC,SIMPLEX,MULTICAST> mtu 1500
	options=460<TSO4,TSO6,CHANNEL_IO>
	ether 36:a1:0b:22:33:40
	media: autoselect <full-duplex>
	status: inactive
utun0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
	inet6 fe80::ce81:b1c:bd2c:69e%utun0 prefixlen 64 scopeid 0x10
	nd6 options=201<PERFORMNUD,DAD>
"""

HARDWARE_PORTS = """\

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 3c:22:fb:12:34:56

Hardware Port: Thunderbolt 1
Device: en1
Ethernet Address: 36:a1:0b:22:33:40

Hardware Port: Thunderbolt Bridge
Device: bridge0
Ethernet Address: N/A

Hardware Port: USB 10/100/1000 LAN
Device: en5
Ethernet Address: 00:e0:4c:68:01:02

VLAN Configurations
===================
"""

GETPACKET = """\
op = BOOTREPLY
htype = 1
flags = 0
hlen = 6
hops = 0
xid = 0x6f3a2b1c
secs = 0
ciaddr = 0.0.0.0
yiaddr = 10.20.30.40
siaddr = 10.20.30.1
giaddr = 0.0.0.0
chaddr = 00:e0:4c:68:01:02
sname =
file =
options:
Options count is 6
dhcp_message_type (uint8): ACK 0x5
server_identifier (ip): 10.20.30.1
lease_time (uint32): 0x15180
subnet_mask (ip): 255.255.255.0
router (ip_mult): {10.20.30.1}
domain_name_server (ip_mult): {10.20.30.1}
end (none):
"""


def test_parse_ifconfig():
    ifs = M.parse_ifconfig(IFCONFIG_A)
    assert set(ifs) == {"lo0", "en0", "en5", "bridge0", "en1", "utun0"}
    en5 = ifs["en5"]
    assert en5["mac"] == "00:e0:4c:68:01:02"
    assert en5["ipv4"] == ["169.254.33.44/16"]
    assert en5["ipv6ll"] == ["fe80::10f3:5b1f:9d2e:abcd"]
    assert en5["status"] == "active"
    assert M.media_speed(en5["media"]) == 1000
    assert ifs["en0"]["ipv4"] == ["192.168.1.23/24"]
    assert ifs["lo0"]["ipv4"] == ["127.0.0.1/8"]
    assert ifs["bridge0"]["status"] == "inactive"
    assert ifs["utun0"]["mac"] == ""
    assert "UP" in ifs["en5"]["flags"]


def test_media_speed():
    assert M.media_speed("autoselect (10GbaseT <full-duplex>)") == 10000
    assert M.media_speed("autoselect (2500Base-T <full-duplex>)") == 2500
    assert M.media_speed("autoselect (100baseTX <full-duplex,flow-control>)") == 100
    assert M.media_speed("autoselect") == 0


def test_hardware_ports_and_kinds():
    ports = M.parse_hardware_ports(HARDWARE_PORTS)
    assert ports == {"en0": "Wi-Fi", "en1": "Thunderbolt 1", "bridge0": "Thunderbolt Bridge",
                     "en5": "USB 10/100/1000 LAN"}
    assert M.port_kind("Wi-Fi") == "wifi"
    assert M.port_kind("USB 10/100/1000 LAN") == "ethernet"
    assert M.port_kind("Ethernet") == "ethernet"
    assert M.port_kind("以太网") == "ethernet"
    assert M.port_kind("Thunderbolt Ethernet Slot 1") == "ethernet"
    assert M.port_kind("Thunderbolt 1") == "virtual"
    assert M.port_kind("雷雳网桥") == "virtual"
    assert M.port_kind("iPhone USB") == "virtual"
    assert M.port_kind("Bluetooth PAN") == "virtual"


def test_parse_getpacket():
    pkt = M.parse_getpacket(GETPACKET)
    assert pkt["yiaddr"] == "10.20.30.40"
    assert pkt["server_identifier"] == "10.20.30.1"
    assert pkt["router"] == "10.20.30.1"


class FakeMac:
    """模拟 ifconfig / ipconfig / ping / socketfilterfw。"""

    def __init__(self, lease=None, gateway_up=False, firewall=False):
        self.ifs = M.parse_ifconfig(IFCONFIG_A)
        self.lease = lease
        self.gateway_up = gateway_up
        self.firewall = firewall
        self.fw_apps = set()
        self.calls = []

    def render(self, name):
        info = self.ifs[name]
        lines = ["%s: flags=8863<%s> mtu 1500" % (name, ",".join(info["flags"]))]
        if info["mac"]:
            lines.append("\tether %s" % info["mac"])
        for ll in info["ipv6ll"]:
            lines.append("\tinet6 %s%%%s prefixlen 64 scopeid 0x13 " % (ll, name))
        for c in info["ipv4"]:
            i = ipaddress.ip_interface(c)
            lines.append("\tinet %s netmask 0x%08x broadcast %s" % (
                i.ip, int(i.netmask), i.network.broadcast_address))
        if info["status"]:
            lines.append("\tstatus: %s" % info["status"])
        return "\n".join(lines) + "\n"

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[0] == M.IFCONFIG:
            if cmd[1] == "-a":
                return 0, "".join(self.render(n) for n in self.ifs)
            name = cmd[1]
            if name not in self.ifs:
                return 1, "ifconfig: interface %s does not exist" % name
            if len(cmd) == 2:
                return 0, self.render(name)
            if cmd[2] in ("up", "down"):
                self.ifs[name]["status"] = "active" if cmd[2] == "up" else "inactive"
                return 0, ""
            if cmd[2] == "inet" and cmd[-1] == "alias":
                cidr = "%s/%d" % (cmd[3], M.mask_prefix(cmd[5]))
                self.ifs[name]["ipv4"].append(cidr)
                return 0, ""
            if cmd[2] == "inet" and cmd[-1] == "-alias":
                self.ifs[name]["ipv4"] = [c for c in self.ifs[name]["ipv4"] if c.split("/")[0] != cmd[3]]
                return 0, ""
        if cmd[0] == M.IPCONFIG:
            return (0, GETPACKET) if self.lease else (1, "")
        if cmd[0] == M.PING:
            return (0, "") if self.gateway_up else (2, "")
        if cmd[0] == M.SOCKETFILTERFW:
            if cmd[1] == "--getglobalstate":
                return 0, "Firewall is %s. (State = %d)" % (
                    "enabled" if self.firewall else "disabled", int(self.firewall))
            if cmd[1] == "--getblockall":
                return 0, "Firewall has block all state set to disabled."
            if cmd[1] == "--getappblocked":
                known = cmd[2] in self.fw_apps
                return 0, "The application %s %s" % (cmd[2], "is permitted" if known else "is not part of the firewall")
            if cmd[1] == "--add":
                self.fw_apps.add(cmd[2])
            if cmd[1] == "--remove":
                self.fw_apps.discard(cmd[2])
            return 0, ""
        return 127, "unexpected %s" % cmd


def test_mac_link_up_and_down(tmp_path):
    fake = FakeMac(firewall=True)
    state = str(tmp_path / "links.json")
    logs = []
    link = M.MacLink("en5", logs.append, runner=fake, state_file=state, exe="/Applications/AutoRustDesk.app/x")
    r = link.up("192.168.77.1/24")
    assert r["backend"] == "ifconfig" and r["carrier"] is True and r["foreign_dhcp"] is False
    assert "192.168.77.1/24" in fake.ifs["en5"]["ipv4"]
    assert "169.254.33.44/16" in fake.ifs["en5"]["ipv4"]  # 系统自己的地址不动
    assert fake.fw_apps == {"/Applications/AutoRustDesk.app/x"}
    rec = json.load(open(state, encoding="utf-8"))["en5"]
    assert rec["addresses"] == ["192.168.77.1/24"] and rec["fw_app"]
    # 再加一个 B 网段的地址
    assert link.add_address("10.9.8.254/24")
    assert not link.add_address("10.9.8.254/24")
    # 地址丢失后 ensure 补回来
    fake.ifs["en5"]["ipv4"].remove("10.9.8.254/24")
    assert link.ensure()
    assert "10.9.8.254/24" in fake.ifs["en5"]["ipv4"]
    link.down()
    assert fake.ifs["en5"]["ipv4"] == ["169.254.33.44/16"]
    assert fake.fw_apps == set()
    assert json.load(open(state, encoding="utf-8")) == {}


def test_mac_link_rejects_conflicting_subnet(tmp_path):
    fake = FakeMac()
    link = M.MacLink("en5", lambda m: None, runner=fake, state_file=str(tmp_path / "s.json"))
    try:
        link.up("192.168.1.1/24")  # 与 Wi-Fi（en0）的网段冲突
    except M.LinkError as e:
        assert "en0" in str(e)
    else:
        raise AssertionError("应当报网段冲突")


def test_mac_foreign_dhcp_needs_live_lease(tmp_path):
    fake = FakeMac(lease=True, gateway_up=True)
    link = M.MacLink("en5", lambda m: None, runner=fake, state_file=str(tmp_path / "s.json"))
    # 租约里的地址不在网卡上：旧租约，不算
    assert link.up("192.168.77.1/24")["foreign_dhcp"] is False
    link.down()
    fake.ifs["en5"]["ipv4"].append("10.20.30.40/24")
    assert link.up("192.168.77.1/24")["foreign_dhcp"] is True
    link.down()
    fake.gateway_up = False  # 网关不通：不是局域网
    assert link.up("192.168.77.1/24")["foreign_dhcp"] is False


def test_mac_cleanup_all_restores_recorded_changes(tmp_path):
    fake = FakeMac()
    state = str(tmp_path / "links.json")
    link = M.MacLink("en5", lambda m: None, runner=fake, state_file=state)
    link.up("192.168.77.1/24")
    # 模拟助手崩溃：没有调用 down()，下次启动时清理
    done = M.cleanup_all(lambda m: None, runner=fake, state_file=state)
    assert done == ["en5"]
    assert "192.168.77.1/24" not in fake.ifs["en5"]["ipv4"]


def test_mac_bounce_takes_link_down_and_up(tmp_path, monkeypatch):
    fake = FakeMac()
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    link = M.MacLink("en5", lambda m: None, runner=fake, state_file=str(tmp_path / "s.json"))
    link.up("192.168.77.1/24")
    assert link.bounce("long") == "关闭网口 7 秒"
    downs = [c for c in fake.calls if c[:3] == [M.IFCONFIG, "en5", "down"]]
    assert downs and fake.ifs["en5"]["status"] == "active"
