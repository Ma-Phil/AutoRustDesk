"""Windows 网卡操作：结构体布局、解析系统返回的数据、netsh 调用顺序（用假的执行器，在任何系统上都能测）。"""

import ctypes
import json
import socket
import struct

import pytest

from autorustdesk.helper import os_windows as W
from autorustdesk.helper import winnet as N


def test_struct_layouts_match_windows_sdk():
    # 64 位 Windows SDK 中的大小和偏移
    assert ctypes.sizeof(N.IP_ADAPTER_ADDRESSES) == 448
    assert N.IP_ADAPTER_ADDRESSES.FriendlyName.offset == 72
    assert N.IP_ADAPTER_ADDRESSES.IfType.offset == 100
    assert N.IP_ADAPTER_ADDRESSES.ReceiveLinkSpeed.offset == 192
    assert N.IP_ADAPTER_ADDRESSES.FirstGatewayAddress.offset == 208
    assert N.IP_ADAPTER_ADDRESSES.Dhcpv4Server.offset == 232
    assert ctypes.sizeof(N.IP_ADAPTER_UNICAST_ADDRESS) == 64
    assert N.IP_ADAPTER_UNICAST_ADDRESS.OnLinkPrefixLength.offset == 56
    assert ctypes.sizeof(N.IP_ADAPTER_GATEWAY_ADDRESS) == 32
    assert ctypes.sizeof(N.MIB_IPNET_ROW2) == 88
    assert N.MIB_IPNET_ROW2.PhysicalAddressLength.offset == 72
    assert N.MIB_IPNET_TABLE2.Table.offset == 8
    assert ctypes.sizeof(N.SHELLEXECUTEINFOW) == 112
    assert N.SHELLEXECUTEINFOW.hProcess.offset == 104


def sockaddr4(ip):
    return ctypes.create_string_buffer(struct.pack("<HH4s8x", N.AF_INET, 0, socket.inet_aton(ip)), 16)


def sockaddr6(ip, scope=0):
    raw = struct.pack("<HHI16sI", N.AF_INET6, 0, 0, socket.inet_pton(socket.AF_INET6, ip), scope)
    return ctypes.create_string_buffer(raw, 28)


def test_adapter_info_parses_addresses_gateways_and_flags():
    keep = []
    a = N.IP_ADAPTER_ADDRESSES()
    a.IfIndex = 12
    a.Ipv6IfIndex = 12
    a.AdapterName = b"{4D36E972-E325-11CE-BFC1-08002BE10318}"
    a.FriendlyName = "以太网 2"
    a.Description = "Realtek USB GbE Family Controller"
    for i, b in enumerate(bytes.fromhex("00e04c680102")):
        a.PhysicalAddress[i] = b
    a.PhysicalAddressLength = 6
    a.IfType = N.IF_TYPE_ETHERNET_CSMACD
    a.OperStatus = N.IF_OPER_STATUS_UP
    a.Flags = N.IP_ADAPTER_DHCP_ENABLED | 0x80
    a.ReceiveLinkSpeed = 1000 * 1000 * 1000

    addrs = [(sockaddr4("10.20.30.40"), 24, N.IP_PREFIX_ORIGIN_DHCP, N.IP_DAD_STATE_PREFERRED),
             (sockaddr4("192.168.77.1"), 24, 1, 1),  # 刚配置，还在重复地址检测（暂定）
             (sockaddr6("fe80::10f3:5b1f:9d2e:abcd", 12), 64, 4, N.IP_DAD_STATE_PREFERRED)]
    nodes = []
    for buf, plen, origin, dad in addrs:
        u = N.IP_ADAPTER_UNICAST_ADDRESS()
        u.Address.lpSockaddr = ctypes.addressof(buf)
        u.OnLinkPrefixLength = plen
        u.PrefixOrigin = origin
        u.DadState = dad
        nodes.append(u)
        keep.append(buf)
    for u, nxt in zip(nodes, nodes[1:]):
        u.Next = ctypes.pointer(nxt)
    a.FirstUnicastAddress = ctypes.pointer(nodes[0])
    gw_buf = sockaddr4("10.20.30.1")
    gw = N.IP_ADAPTER_GATEWAY_ADDRESS()
    gw.Address.lpSockaddr = ctypes.addressof(gw_buf)
    a.FirstGatewayAddress = ctypes.pointer(gw)
    dhcp_buf = sockaddr4("10.20.30.1")
    a.Dhcpv4Server.lpSockaddr = ctypes.addressof(dhcp_buf)

    info = N.adapter_info(a)
    assert info["name"] == "以太网 2"
    assert info["guid"] == "{4D36E972-E325-11CE-BFC1-08002BE10318}"
    assert info["index"] == 12 and info["ipv6_index"] == 12
    assert info["mac"] == "00:e0:4c:68:01:02"
    assert info["up"] and info["dhcp"] and info["speed"] == 1000
    assert info["ipv4"] == ["10.20.30.40/24", "192.168.77.1/24"]
    assert info["ipv4_dhcp"] == ["10.20.30.40/24"]
    assert info["ipv4_ready"] == ["10.20.30.40/24"]  # 暂定的地址还不能绑定
    assert info["ipv6ll"] == ["fe80::10f3:5b1f:9d2e:abcd"]
    assert info["gateways"] == ["10.20.30.1"]
    assert info["dhcp_server"] == "10.20.30.1"
    assert not N.looks_virtual(info)
    assert N.looks_virtual({"name": "vEthernet (WSL)", "description": "Hyper-V Virtual Ethernet Adapter"})
    assert N.looks_virtual({"name": "以太网 3", "description": "TAP-Windows Adapter V9"})


def test_neighbor_info():
    row = N.MIB_IPNET_ROW2()
    raw = struct.pack("<HH4s", N.AF_INET, 0, socket.inet_aton("192.168.77.100"))
    ctypes.memmove(row.Address, raw, len(raw))
    for i, b in enumerate(bytes.fromhex("525400123456")):
        row.PhysicalAddress[i] = b
    row.PhysicalAddressLength = 6
    row.InterfaceIndex = 12
    row.State = N.NLNS_STALE
    assert N.neighbor_info(row) == {"ip": "192.168.77.100", "mac": "52:54:00:12:34:56",
                                    "ifindex": 12, "state": N.NLNS_STALE}
    raw6 = struct.pack("<HHI16s", N.AF_INET6, 0, 0, socket.inet_pton(socket.AF_INET6, "fe80::5054:ff:fe12:3456"))
    ctypes.memmove(row.Address, raw6, len(raw6))
    assert N.neighbor_info(row)["ip"] == "fe80::5054:ff:fe12:3456"
    row.PhysicalAddressLength = 0  # 还没解析出 MAC
    assert N.neighbor_info(row) is None


class FakeWindows:
    """模拟 netsh / ping 和网卡列表。"""

    def __init__(self, dhcp=True, lease=None, gateway_up=False):
        self.adapters = [
            {"name": "以太网 2", "description": "Realtek USB GbE", "guid": "{AAAA}", "index": 12,
             "ipv6_index": 12, "mac": "00:e0:4c:68:01:02", "type": 6, "up": True, "dhcp": dhcp,
             "speed": 1000, "ipv4": [], "ipv4_dhcp": [], "ipv6ll": ["fe80::1"], "ipv6": [],
             "gateways": [], "dhcp_server": ""},
            {"name": "WLAN", "description": "Intel Wi-Fi 6", "guid": "{BBBB}", "index": 7,
             "ipv6_index": 7, "mac": "3c:22:fb:12:34:56", "type": 71, "up": True, "dhcp": True,
             "speed": 866, "ipv4": ["192.168.1.23/24"], "ipv4_dhcp": ["192.168.1.23/24"], "ipv6ll": [],
             "ipv6": [], "gateways": ["192.168.1.1"], "dhcp_server": "192.168.1.1"},
        ]
        eth = self.adapters[0]
        if lease:
            eth["ipv4"] = [lease]
            eth["ipv4_dhcp"] = [lease]
            eth["gateways"] = ["10.20.30.1"]
        elif dhcp:
            eth["ipv4"] = ["169.254.33.44/16"]
        else:
            eth["ipv4"] = ["10.1.1.5/24"]
        self.gateway_up = gateway_up
        self.fw_rules = []
        self.calls = []

    def adapter(self, name):
        return next((a for a in self.adapters if a["name"] == name), None)

    def by_index(self, arg):
        idx = int(arg.split("=")[1])
        return next(a for a in self.adapters if a["index"] == idx)

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:3] == ["netsh", "interface", "ipv4"]:
            op, what = cmd[3], cmd[4]
            a = self.by_index(cmd[5])
            args = dict(x.split("=", 1) for x in cmd[6:])
            if op == "set" and what == "address" and args.get("source") == "static":
                assert args["gateway"] == "none"
                a["dhcp"] = False
                a["ipv4"] = ["%s/24" % args["address"]]
                a["ipv4_dhcp"] = []
                return 0, "\r\n"
            if op == "set" and what == "address" and args.get("source") == "dhcp":
                a["dhcp"] = True
                a["ipv4"] = ["169.254.33.44/16"]
                return 0, "\r\n"
            if op == "add":
                a["ipv4"].append("%s/24" % args["address"])
                return 0, "\r\n"
            if op == "delete":
                a["ipv4"] = [c for c in a["ipv4"] if c.split("/")[0] != args["address"]]
                return 0, "\r\n"
        if cmd[:3] == ["netsh", "advfirewall", "firewall"]:
            name = cmd[5].split("=", 1)[1]
            if cmd[3] == "add":
                self.fw_rules.append(name)
            else:
                self.fw_rules = [r for r in self.fw_rules if r != name]
            return 0, "确定。\r\n"
        if cmd[:4] == ["netsh", "interface", "set", "interface"]:
            self.adapter(cmd[4])["up"] = cmd[5] == "admin=enabled"
            return 0, ""
        if cmd[0] == "ping":
            if self.gateway_up:
                return 0, "来自 %s 的回复: 字节=32 时间<1ms TTL=64\r\n" % cmd[-1]
            return 0, "来自 10.20.30.9 的回复: 无法访问目标主机。\r\n"
        return 1, "unexpected %s" % cmd


def make_link(fake, tmp_path, logs=None):
    return W.WinLink("以太网 2", (logs if logs is not None else []).append, runner=fake,
                     adapter_fn=fake.adapter, list_fn=lambda: fake.adapters,
                     state_file=str(tmp_path / "links.json"))


def test_dhcp_adapter_becomes_static_and_is_restored(tmp_path):
    fake = FakeWindows(dhcp=True)
    link = make_link(fake, tmp_path)
    r = link.up("192.168.77.1/24")
    assert r["backend"] == "netsh" and r["foreign_dhcp"] is False
    eth = fake.adapter("以太网 2")
    assert eth["ipv4"] == ["192.168.77.1/24"] and not eth["dhcp"]
    assert fake.fw_rules == [W.FW_RULE]
    rec = json.load(open(tmp_path / "links.json", encoding="utf-8"))["以太网 2"]
    assert rec["was_dhcp"] and rec["index"] == 12 and rec["guid"] == "{AAAA}"
    assert link.add_address("10.9.8.254/24")
    link.down()
    assert eth["dhcp"] and eth["ipv4"] == ["169.254.33.44/16"]
    assert fake.fw_rules == []
    assert json.load(open(tmp_path / "links.json", encoding="utf-8")) == {}
    # Wi-Fi 没有被碰过
    assert all(c[5] == "name=12" for c in fake.calls if c[:3] == ["netsh", "interface", "ipv4"])


def test_static_adapter_only_gets_an_extra_address(tmp_path):
    fake = FakeWindows(dhcp=False)
    link = make_link(fake, tmp_path)
    link.up("192.168.77.1/24")
    eth = fake.adapter("以太网 2")
    assert eth["ipv4"] == ["10.1.1.5/24", "192.168.77.1/24"]
    link.down()
    assert eth["ipv4"] == ["10.1.1.5/24"] and not eth["dhcp"]
    assert not any("source=dhcp" in c for c in fake.calls)


def test_crash_leftovers_are_restored_by_guid(tmp_path):
    fake = FakeWindows(dhcp=True)
    link = make_link(fake, tmp_path)
    link.up("192.168.77.1/24")
    fake.adapters[0]["index"] = 19  # USB 网卡换了插口，序号变了
    done = W.cleanup_all(lambda m: None, runner=fake, list_fn=lambda: fake.adapters,
                         state_file=str(tmp_path / "links.json"))
    assert done == ["以太网 2"]
    assert ["netsh", "interface", "ipv4", "set", "address", "name=19", "source=dhcp"] in fake.calls
    assert fake.adapters[0]["dhcp"]


def test_conflicting_subnet_is_rejected(tmp_path):
    fake = FakeWindows()
    link = make_link(fake, tmp_path)
    with pytest.raises(W.LinkError, match="WLAN"):
        link.up("192.168.1.1/24")
    assert fake.adapter("以太网 2")["dhcp"]  # 没有改动


def test_live_dhcp_lease_means_lan(tmp_path):
    fake = FakeWindows(lease="10.20.30.40/24", gateway_up=True)
    link = make_link(fake, tmp_path)
    assert link.up("192.168.77.1/24")["foreign_dhcp"] is True
    link.down()
    fake2 = FakeWindows(lease="10.20.30.40/24", gateway_up=False)  # 旧租约，网关不通
    link2 = make_link(fake2, tmp_path)
    assert link2.up("192.168.77.1/24")["foreign_dhcp"] is False


def test_bounce_disables_and_enables_adapter(tmp_path, monkeypatch):
    fake = FakeWindows()
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    link = make_link(fake, tmp_path)
    link.up("192.168.77.1/24")
    assert link.bounce("quick") == "停用网卡 2 秒"
    assert ["netsh", "interface", "set", "interface", "以太网 2", "admin=disabled"] in fake.calls
    assert fake.adapter("以太网 2")["up"]
