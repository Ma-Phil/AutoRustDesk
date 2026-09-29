"""电脑 A 为 macOS 时的网卡操作。

- 直连地址用 `ifconfig <网卡> inet <地址> netmask <掩码> alias` 临时加到网卡上：
  不修改"系统设置"里的网络服务，不影响 Wi-Fi，也不设网关；结束时删除。
- 抓包和发 ARP 用系统自带的 libpcap。
- DHCP 服务用绑定到这块网卡（IP_BOUND_IF）的 UDP 套接字。
- 开启了"应用程序防火墙"时，临时允许本程序接收传入连接（DHCP 请求）。
"""

import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

from .backend import Backend, Link, LogFn
from .packetio import PacketIO, PcapIO
from .state import STATE_FILE, load_state, save_state

IFCONFIG = "/sbin/ifconfig"
NETWORKSETUP = "/usr/sbin/networksetup"
IPCONFIG = "/usr/sbin/ipconfig"
PING = "/sbin/ping"
SOCKETFILTERFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"
IP_BOUND_IF = 25  # <netinet/in.h>

Runner = Callable[[List[str]], Tuple[int, str]]


class LinkError(Exception):
    pass


def run(cmd: List[str], timeout: float = 30) -> Tuple[int, str]:
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, env=env)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "%s: 命令不存在" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "%s: 超时" % " ".join(cmd)


# ---------------------------------------------------------------------------
# 解析命令输出
# ---------------------------------------------------------------------------

_IF_HEADER = re.compile(r"^([A-Za-z0-9_.-]+): flags=[0-9a-fA-F]+<([^>]*)>")


def mask_prefix(mask: str) -> int:
    """0xffffff00 或 255.255.255.0 → 24"""
    mask = mask.strip()
    value = int(mask, 16) if mask.lower().startswith("0x") else int(ipaddress.IPv4Address(mask))
    return bin(value).count("1")


def parse_ifconfig(text: str) -> Dict[str, Dict]:
    """解析 ifconfig（-a）的输出。"""
    result: Dict[str, Dict] = {}
    cur: Optional[Dict] = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            m = _IF_HEADER.match(line)
            cur = None
            if m:
                cur = result[m.group(1)] = {
                    "flags": [f for f in m.group(2).split(",") if f], "mac": "",
                    "ipv4": [], "ipv6ll": [], "ipv6": [], "status": "", "media": "",
                }
            continue
        if cur is None:
            continue
        parts = line.split()
        key = parts[0]
        if key == "ether" and len(parts) > 1:
            cur["mac"] = parts[1].lower()
        elif key == "inet" and len(parts) > 1:
            ip, _, prefix = parts[1].partition("/")
            plen = int(prefix) if prefix.isdigit() else 32
            if "netmask" in parts and parts.index("netmask") + 1 < len(parts):
                plen = mask_prefix(parts[parts.index("netmask") + 1])
            cur["ipv4"].append("%s/%d" % (ip, plen))
        elif key == "inet6" and len(parts) > 1:
            try:
                a = ipaddress.IPv6Address(parts[1].split("%")[0])
            except ValueError:
                continue
            cur["ipv6ll" if a.is_link_local else "ipv6"].append(str(a))
        elif key == "status:":
            cur["status"] = parts[1] if len(parts) > 1 else ""
        elif key == "media:":
            cur["media"] = " ".join(parts[1:])
    return result


def media_speed(media: str) -> int:
    """'autoselect (1000baseT <full-duplex>)' → 1000（Mb/s）"""
    m = re.search(r"\((\d+)(G?)[bB]ase", media)
    if not m:
        return 0
    return int(m.group(1)) * (1000 if m.group(2) else 1)


def parse_hardware_ports(text: str) -> Dict[str, str]:
    """networksetup -listallhardwareports → {网卡名: 硬件端口名}"""
    ports: Dict[str, str] = {}
    port: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port is not None:
            ports[line.split(":", 1)[1].strip()] = port
            port = None
    return ports


WIFI_WORDS = ("wi-fi", "wifi", "airport", "wlan", "无线")


def port_kind(port: str) -> str:
    """硬件端口名 → "wifi" / "virtual" / "ethernet"。"""
    p = port.lower()
    if any(w in p for w in WIFI_WORDS):
        return "wifi"
    ethernet = "ethernet" in p or "以太网" in p or "lan" in p
    if ("thunderbolt" in p or "雷雳" in p) and not ethernet:
        return "virtual"  # 雷雳网桥 / 雷雳 1：电脑之间直连用的，不是网口
    if any(w in p for w in ("bluetooth", "蓝牙", "bridge", "网桥", "iphone", "ipad", "vpn", "vlan")):
        return "virtual"
    return "ethernet"


def parse_getpacket(text: str) -> Dict[str, str]:
    """ipconfig getpacket 的输出 → {"yiaddr": ..., "server_identifier": ..., "router": ...}"""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(yiaddr|siaddr)\s*=\s*(\S+)", line)
        if m:
            out[m.group(1)] = m.group(2)
            continue
        m = re.match(r"^(server_identifier|router)\s*\([^)]*\):\s*\{?([0-9.]+)", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


# ---------------------------------------------------------------------------
# 网卡配置
# ---------------------------------------------------------------------------


class MacLink(Link):
    def __init__(self, iface: str, log: LogFn, runner: Runner = run, state_file: str = STATE_FILE,
                 exe: str = ""):
        self.iface = iface
        self.log = log
        self.run = runner
        self.state_file = state_file
        self.exe = exe or os.path.realpath(sys.executable)
        self.primary = ""
        self.addresses: List[str] = []
        self.fw_app = ""

    def info(self) -> Dict:
        rc, out = self.run([IFCONFIG, self.iface])
        return parse_ifconfig(out).get(self.iface, {}) if rc == 0 else {}

    def _record(self) -> None:
        state = load_state(self.state_file)
        state[self.iface] = {"backend": "ifconfig", "addresses": self.addresses, "fw_app": self.fw_app}
        save_state(state, self.state_file)

    def _forget(self) -> None:
        state = load_state(self.state_file)
        state.pop(self.iface, None)
        save_state(state, self.state_file)

    def up(self, cidr: str) -> Dict:
        want = ipaddress.ip_interface(cidr)
        if not (want.ip.is_private or want.ip.is_link_local):
            raise LinkError("直连网段必须是私有地址：%s" % cidr)
        rc, out = self.run([IFCONFIG, "-a"])
        all_ifs = parse_ifconfig(out)
        if self.iface not in all_ifs:
            raise LinkError("网卡不存在：%s" % self.iface)
        for name, info in all_ifs.items():
            if name == self.iface or name.startswith("lo"):
                continue
            for c in info["ipv4"]:
                net = ipaddress.ip_interface(c).network
                if net.overlaps(want.network):
                    raise LinkError("直连网段 %s 与本机网卡 %s 的网段 %s 冲突，请在设置里换一个" % (
                        want.network, name, net))
        foreign = self._live_dhcp_lease(all_ifs[self.iface])
        self.run([IFCONFIG, self.iface, "up"])
        self.primary = str(want)
        self.add_address(str(want))
        self._firewall_allow()
        self._record()
        self.log("已在 %s 上添加直连地址 %s（不设网关，不修改系统网络设置）" % (self.iface, want))
        info = self.info()
        return {"backend": "ifconfig", "cidr": str(want), "mac": info.get("mac", ""),
                "addresses": {k: info.get(k, []) for k in ("ipv4", "ipv6ll", "ipv6")},
                "carrier": info.get("status") == "active", "foreign_dhcp": foreign}

    def _live_dhcp_lease(self, info: Dict) -> bool:
        """网卡当前有别的 DHCP 服务器分配的地址、而且网关能 ping 通：插的是局域网。"""
        rc, out = self.run([IPCONFIG, "getpacket", self.iface])
        if rc != 0:
            return False
        pkt = parse_getpacket(out)
        yiaddr = pkt.get("yiaddr", "")
        if not yiaddr or not any(c.split("/")[0] == yiaddr for c in info.get("ipv4", [])):
            return False  # 旧租约，地址已经不在网卡上了
        router = pkt.get("router")
        if not router:
            return True
        rc, _ = self.run([PING, "-c", "1", "-t", "2", router])
        return rc == 0

    def _alias(self, cidr: str) -> Tuple[int, str]:
        want = ipaddress.ip_interface(cidr)
        return self.run([IFCONFIG, self.iface, "inet", str(want.ip), "netmask", str(want.netmask), "alias"])

    def add_address(self, cidr: str) -> bool:
        want = ipaddress.ip_interface(cidr)
        present = [ipaddress.ip_interface(a) for a in self.info().get("ipv4", [])]
        if want in present:
            return False
        rc, out = self._alias(str(want))
        if rc != 0:
            raise LinkError("添加地址 %s 失败：%s" % (want, out.strip()))
        self.addresses.append(str(want))
        self._record()
        return True

    def ensure(self) -> bool:
        if not self.addresses:
            return False
        present = {str(ipaddress.ip_interface(a)) for a in self.info().get("ipv4", [])}
        missing = [c for c in self.addresses if c not in present]
        for cidr in missing:
            self._alias(cidr)
        return bool(missing)

    def _wait_carrier(self, up: bool, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if (self.info().get("status") == "active") == up:
                return True
            time.sleep(0.3)
        return False

    def bounce(self, mode: str = "quick") -> str:
        seconds = 7 if mode == "long" else 2
        self.run([IFCONFIG, self.iface, "down"])
        time.sleep(seconds)
        self.run([IFCONFIG, self.iface, "up"])
        self._wait_carrier(True, 15)
        self.ensure()
        return "关闭网口 %d 秒" % seconds

    def _firewall_allow(self) -> None:
        """应用程序防火墙开启时，允许本程序接收 DHCP 请求。"""
        rc, out = self.run([SOCKETFILTERFW, "--getglobalstate"])
        if rc != 0 or "enabled" not in out.lower():
            return
        rc, out = self.run([SOCKETFILTERFW, "--getblockall"])
        if rc == 0 and "enabled" in out.lower():
            self.log("警告：macOS 防火墙设置了“阻止所有传入连接”，B 的 DHCP 请求可能收不到；"
                     "如果找不到 B，请在“系统设置 → 网络 → 防火墙”里暂时关闭该选项")
        rc, out = self.run([SOCKETFILTERFW, "--getappblocked", self.exe])
        if "not part of" in out.lower():
            if self.run([SOCKETFILTERFW, "--add", self.exe])[0] == 0:
                self.fw_app = self.exe
        self.run([SOCKETFILTERFW, "--unblockapp", self.exe])
        self.log("已在 macOS 防火墙中临时允许本程序接收 DHCP 请求")

    def down(self) -> None:
        restore(self.iface, {"addresses": self.addresses, "fw_app": self.fw_app}, self.log, self.run)
        self.addresses = []
        self.fw_app = ""
        self._forget()


def restore(iface: str, rec: Dict, log: LogFn, runner: Runner = run) -> None:
    for cidr in rec.get("addresses") or []:
        runner([IFCONFIG, iface, "inet", cidr.split("/")[0], "-alias"])
    if rec.get("fw_app"):
        runner([SOCKETFILTERFW, "--remove", rec["fw_app"]])
    log("已恢复网卡 %s 的原有设置" % iface)


def cleanup_all(log: LogFn, runner: Runner = run, state_file: str = STATE_FILE) -> List[str]:
    state = load_state(state_file)
    done = []
    for iface, rec in list(state.items()):
        restore(iface, rec, log, runner)
        done.append(iface)
    save_state({}, state_file)
    return done


class MacBackend(Backend):
    name = "macos"
    # 绑定了网卡（IP_BOUND_IF）的套接字发往 255.255.255.255 会报"网络不可达"
    dhcp_subnet_broadcast = True

    def _info(self, iface: str) -> Dict:
        if not iface or "/" in iface:
            return {}
        rc, out = run([IFCONFIG, iface])
        return parse_ifconfig(out).get(iface, {}) if rc == 0 else {}

    def is_admin(self) -> bool:
        return os.geteuid() == 0

    def iface_exists(self, iface: str) -> bool:
        return bool(self._info(iface))

    def iface_mac(self, iface: str) -> str:
        return self._info(iface).get("mac", "")

    def iface_addresses(self, iface: str) -> Dict[str, List[str]]:
        info = self._info(iface)
        return {k: info.get(k, []) for k in ("ipv4", "ipv6ll", "ipv6")}

    def carrier(self, iface: str) -> Optional[bool]:
        status = self._info(iface).get("status", "")
        return None if not status else status == "active"

    def link(self, iface: str, log: LogFn) -> Link:
        return MacLink(iface, log)

    def cleanup_all(self, log: LogFn) -> List[str]:
        return cleanup_all(log)

    def packet_io(self, iface: str, send_only: bool = False) -> PacketIO:
        return PcapIO(iface, send_only)

    def dhcp_socket(self, iface: str, server_ip: str) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # 只收发这块网卡上的报文（相当于 Linux 的 SO_BINDTODEVICE）
            s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(iface))
            s.bind(("0.0.0.0", 67))
        except OSError:
            s.close()
            raise
        return s
