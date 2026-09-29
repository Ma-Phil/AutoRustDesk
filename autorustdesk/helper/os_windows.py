"""电脑 A 为 Windows 时的网卡操作。

- 直连地址用 netsh 配置：网卡原来是"自动获得 IP 地址"时，临时改成固定地址（不设网关），
  结束时改回自动获得；原来就是固定地址时只追加一个地址，结束时删除。
  改动写进系统之前先记下来，助手意外退出后下次启动会恢复。
- 防火墙临时放行 UDP 67（B 的 DHCP 请求）。
- 抓包用 Npcap（需要自行安装）；没装 Npcap 时靠 DHCP 分配记录、系统邻居表、SendARP
  和 IPv6 探测发现 B。
- 网卡用"网络连接"里显示的名称（如"以太网 2"）标识；netsh 命令里用网卡序号，避免名称编码问题。
"""

import ipaddress
import socket
import struct
import subprocess
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import winnet
from .backend import Backend, Link, LogFn
from .packetio import PacketIO, PacketIOError, PcapIO
from .state import STATE_FILE, load_state, save_state

FW_RULE = "AutoRustDesk-DHCP"
CREATE_NO_WINDOW = 0x08000000
IP_UNICAST_IF = 31

Runner = Callable[[List[str]], Tuple[int, str]]
AdapterFn = Callable[[str], Optional[Dict]]


class LinkError(Exception):
    pass


def run(cmd: List[str], timeout: float = 60) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return p.returncode, p.stdout.decode(winnet.oem_encoding(), "replace")
    except FileNotFoundError:
        return 127, "%s: 命令不存在" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "%s: 超时" % " ".join(cmd)


def ping(ip: str, runner: Runner = run) -> bool:
    # Windows 的 ping 收到"无法访问目标主机"时也可能返回 0，所以看有没有 TTL=
    rc, out = runner(["ping", "-n", "1", "-w", "1000", ip])
    return rc == 0 and "TTL=" in out.upper()


def netsh_ipv4(*args: str) -> List[str]:
    return ["netsh", "interface", "ipv4"] + list(args)


class WinLink(Link):
    def __init__(self, iface: str, log: LogFn, runner: Runner = run,
                 adapter_fn: AdapterFn = winnet.adapter, list_fn: Callable[[], List[Dict]] = winnet.adapters,
                 state_file: str = STATE_FILE):
        self.iface = iface
        self.log = log
        self.run = runner
        self.adapter_fn = adapter_fn
        self.list_fn = list_fn
        self.state_file = state_file
        a = self._adapter()
        self.index = a["index"]
        self.guid = a["guid"]
        self.primary = ""
        self.was_dhcp = False  # 原来是自动获取地址，已临时改为固定地址
        self.addresses: List[str] = []  # 追加的地址（结束时删除）
        self.fw_rule = False

    def _adapter(self) -> Dict:
        a = self.adapter_fn(self.iface)
        if not a:
            raise LinkError("找不到网卡：%s" % self.iface)
        return a

    # ------------------------------------------------------------------ 记录

    def _rec(self) -> Dict:
        return {"backend": "netsh", "index": self.index, "guid": self.guid, "was_dhcp": self.was_dhcp,
                "addresses": list(self.addresses), "fw_rule": self.fw_rule}

    def _record(self) -> None:
        state = load_state(self.state_file)
        state[self.iface] = self._rec()
        save_state(state, self.state_file)

    def _forget(self) -> None:
        state = load_state(self.state_file)
        state.pop(self.iface, None)
        save_state(state, self.state_file)

    # ------------------------------------------------------------------ 配置

    def up(self, cidr: str) -> Dict:
        want = ipaddress.ip_interface(cidr)
        if not (want.ip.is_private or want.ip.is_link_local):
            raise LinkError("直连网段必须是私有地址：%s" % cidr)
        for other in self.list_fn():
            if other["guid"] == self.guid:
                continue
            for c in other["ipv4"]:
                net = ipaddress.ip_interface(c).network
                if not net.is_link_local and net.overlaps(want.network):
                    raise LinkError("直连网段 %s 与本机网卡“%s”的网段 %s 冲突，请在设置里换一个" % (
                        want.network, other["name"], net))
        a = self._adapter()
        foreign = self._live_dhcp_lease(a)
        self.primary = str(want)
        if a["dhcp"]:
            # 先记下来再改：万一中途退出，下次启动能改回"自动获得 IP 地址"
            self.was_dhcp = True
            self._record()
            rc, out = self.run(netsh_ipv4("set", "address", "name=%d" % self.index, "source=static",
                                          "address=%s" % want.ip, "mask=%s" % want.netmask, "gateway=none"))
            if rc != 0:
                self.was_dhcp = False
                self._record()
                raise LinkError("配置直连地址失败：%s" % out.strip())
            self.log("已把“%s”临时改为固定地址 %s（不设网关，结束后改回自动获得）" % (self.iface, want))
        else:
            self.add_address(str(want))
            self.log("已在“%s”上添加直连地址 %s（不设网关）" % (self.iface, want))
        self._firewall_allow()
        self._record()
        if not self._wait_address(str(want.ip), 10):
            self.log("警告：直连地址 %s 还没有生效（网线没插好？）" % want.ip)
        a = self._adapter()
        return {"backend": "netsh", "cidr": str(want), "mac": a["mac"],
                "addresses": {k: a[k] for k in ("ipv4", "ipv6ll", "ipv6")},
                "carrier": a["up"], "foreign_dhcp": foreign}

    def _live_dhcp_lease(self, a: Dict) -> bool:
        """网卡当前有别的 DHCP 服务器分配的地址、而且网关能 ping 通：插的是局域网。"""
        if not a["dhcp"]:
            return False
        leased = [c for c in a["ipv4_dhcp"] if not c.startswith("169.254.")]
        if not leased:
            return False
        gateways = [g for g in a["gateways"] if ":" not in g]
        if not gateways:
            return True
        return any(ping(g, self.run) for g in gateways)

    def _wait_address(self, ip: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while True:
            a = self.adapter_fn(self.iface)
            if a and any(c.split("/")[0] == ip for c in a["ipv4"]):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.5)

    def add_address(self, cidr: str) -> bool:
        want = ipaddress.ip_interface(cidr)
        a = self._adapter()
        if any(ipaddress.ip_interface(c) == want for c in a["ipv4"]):
            return False
        self.addresses.append(str(want))
        self._record()
        rc, out = self.run(netsh_ipv4("add", "address", "name=%d" % self.index,
                                      "address=%s" % want.ip, "mask=%s" % want.netmask))
        if rc != 0 and not self._wait_address(str(want.ip), 2):
            self.addresses.remove(str(want))
            self._record()
            raise LinkError("添加地址 %s 失败：%s" % (want, out.strip()))
        return True

    def ensure(self) -> bool:
        """Windows 的地址配置保存在系统里，网线拔插后会自动恢复；只在确实丢失时重新配置。"""
        if not self.primary:
            return False
        ip = self.primary.split("/")[0]
        if self._wait_address(ip, 5):
            return False
        want = ipaddress.ip_interface(self.primary)
        if self.was_dhcp:
            self.run(netsh_ipv4("set", "address", "name=%d" % self.index, "source=static",
                                "address=%s" % want.ip, "mask=%s" % want.netmask, "gateway=none"))
        for cidr in self.addresses:
            w = ipaddress.ip_interface(cidr)
            self.run(netsh_ipv4("add", "address", "name=%d" % self.index,
                                "address=%s" % w.ip, "mask=%s" % w.netmask))
        return True

    def bounce(self, mode: str = "quick") -> str:
        seconds = 7 if mode == "long" else 2
        self.run(["netsh", "interface", "set", "interface", self.iface, "admin=disabled"])
        time.sleep(seconds)
        self.run(["netsh", "interface", "set", "interface", self.iface, "admin=enabled"])
        deadline = time.time() + 20
        while time.time() < deadline:
            a = self.adapter_fn(self.iface)
            if a and a["up"]:
                break
            time.sleep(0.5)
        if self.primary:
            self._wait_address(self.primary.split("/")[0], 10)
        return "停用网卡 %d 秒" % seconds

    def _firewall_allow(self) -> None:
        self.fw_rule = True
        self._record()
        self.run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=%s" % FW_RULE])
        rc, out = self.run(["netsh", "advfirewall", "firewall", "add", "rule", "name=%s" % FW_RULE,
                            "dir=in", "action=allow", "protocol=UDP", "localport=67", "profile=any"])
        if rc != 0:
            self.fw_rule = False
            self._record()
            self.log("警告：Windows 防火墙放行 DHCP 失败：%s" % out.strip())
        else:
            self.log("Windows 防火墙已临时放行 DHCP 请求（UDP 67）")

    def down(self) -> None:
        restore(self.iface, self._rec(), self.log, self.run, self.list_fn)
        self.addresses = []
        self.was_dhcp = False
        self.fw_rule = False
        self._forget()


def restore(iface: str, rec: Dict, log: LogFn, runner: Runner = run,
            list_fn: Callable[[], List[Dict]] = winnet.adapters) -> None:
    index = rec.get("index")
    # USB 网卡换了插口后序号可能会变，按 GUID 重新找
    try:
        for a in list_fn():
            if rec.get("guid") and a["guid"] == rec["guid"]:
                index = a["index"]
    except OSError:
        pass
    if index:
        for cidr in rec.get("addresses") or []:
            runner(netsh_ipv4("delete", "address", "name=%d" % index, "address=%s" % cidr.split("/")[0]))
        if rec.get("was_dhcp"):
            runner(netsh_ipv4("set", "address", "name=%d" % index, "source=dhcp"))
    if rec.get("fw_rule"):
        runner(["netsh", "advfirewall", "firewall", "delete", "rule", "name=%s" % FW_RULE])
    log("已恢复网卡“%s”的原有设置" % iface)


def cleanup_all(log: LogFn, runner: Runner = run, list_fn: Callable[[], List[Dict]] = winnet.adapters,
                state_file: str = STATE_FILE) -> List[str]:
    state = load_state(state_file)
    done = []
    for iface, rec in list(state.items()):
        restore(iface, rec, log, runner, list_fn)
        done.append(iface)
    save_state({}, state_file)
    return done


class WindowsBackend(Backend):
    name = "windows"
    dhcp_rebind_after_bounce = True

    def _a(self, iface: str) -> Dict:
        return winnet.adapter(iface) or {}

    def is_admin(self) -> bool:
        return winnet.is_admin()

    def iface_exists(self, iface: str) -> bool:
        return bool(iface) and bool(self._a(iface))

    def iface_mac(self, iface: str) -> str:
        return self._a(iface).get("mac", "")

    def iface_addresses(self, iface: str) -> Dict[str, List[str]]:
        a = self._a(iface)
        return {k: a.get(k, []) for k in ("ipv4", "ipv6ll", "ipv6")}

    def ifindex(self, iface: str) -> int:
        return int(self._a(iface).get("ipv6_index") or 0)

    def scope_id(self, iface: str) -> str:
        return str(self._a(iface).get("ipv6_index", ""))

    def carrier(self, iface: str) -> Optional[bool]:
        a = self._a(iface)
        return a.get("up") if a else None

    def link(self, iface: str, log: LogFn) -> Link:
        return WinLink(iface, log)

    def cleanup_all(self, log: LogFn) -> List[str]:
        return cleanup_all(log)

    def packet_io(self, iface: str, send_only: bool = False) -> PacketIO:
        a = self._a(iface)
        if not a:
            raise PacketIOError("找不到网卡：%s" % iface)
        return PcapIO("\\Device\\NPF_" + a["guid"], send_only)

    def dhcp_socket(self, iface: str, server_ip: str) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            index = self._a(iface).get("index")
            if index:
                try:
                    # 广播回复从这块网卡发出（IPv4 的网卡序号要用网络字节序）
                    s.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", index))
                except OSError:
                    pass
            # Windows 上绑定在网卡地址上的套接字能收到这块网卡上的广播，且只收这块网卡的
            s.bind((server_ip, 67))
        except OSError:
            s.close()
            raise
        return s

    def neighbors(self, iface: str) -> List[Dict]:
        index = self._a(iface).get("index")
        return winnet.neighbors(index) if index else []

    def arp_resolve(self, iface: str, target: str, sender: str) -> Optional[str]:
        return winnet.send_arp(target, sender)
