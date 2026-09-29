"""列出电脑 A 的有线网卡（Linux 读 /sys，macOS 用 ifconfig/networksetup，Windows 用 iphlpapi）。"""

import dataclasses
import ipaddress
import json
import os
import socket
import subprocess
import sys
from typing import Dict, List, Optional


@dataclasses.dataclass
class Nic:
    name: str
    mac: str = ""
    carrier: Optional[bool] = None
    operstate: str = ""
    wireless: bool = False
    virtual: bool = False
    driver: str = ""
    bus: str = ""
    speed: int = 0
    ipv4: List[str] = dataclasses.field(default_factory=list)
    desc: str = ""  # macOS 的硬件端口名 / Windows 的网卡型号

    @property
    def label(self) -> str:
        parts = [self.name]
        if self.desc:
            parts.append(self.desc)
        else:
            parts.append({"usb": "USB 网卡", "pci": "有线网卡"}.get(
                self.bus, "虚拟网卡" if self.virtual else "网卡"))
        if self.carrier:
            parts.append("已插网线" + (" %dMb/s" % self.speed if self.speed > 0 else ""))
        elif self.carrier is False:
            parts.append("未插网线")
        if self.ipv4:
            parts.append(", ".join(self.ipv4))
        return " · ".join(parts)


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _ip_addresses() -> Dict[str, List[str]]:
    try:
        out = subprocess.run(["ip", "-j", "addr", "show"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10).stdout
        data = json.loads(out or b"[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    result: Dict[str, List[str]] = {}
    for item in data:
        result[item.get("ifname", "")] = [
            "%s/%s" % (a["local"], a["prefixlen"]) for a in item.get("addr_info", [])
            if a.get("family") == "inet"
        ]
    return result


def list_nics_linux(include_virtual: bool = False) -> List[Nic]:
    base = "/sys/class/net"
    addrs = _ip_addresses()
    nics = []
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        if name == "lo" or _read(os.path.join(path, "type")) != "1":
            continue
        wireless = os.path.exists(os.path.join(path, "wireless")) or os.path.exists(
            os.path.join(path, "phy80211"))
        if wireless:
            continue
        device = os.path.join(path, "device")
        virtual = not os.path.exists(device)
        if virtual and not include_virtual:
            continue
        real = os.path.realpath(device) if not virtual else ""
        carrier_raw = _read(os.path.join(path, "carrier"))
        speed_raw = _read(os.path.join(path, "speed"))
        nics.append(Nic(
            name=name,
            mac=_read(os.path.join(path, "address")).lower(),
            carrier=None if carrier_raw == "" else carrier_raw == "1",
            operstate=_read(os.path.join(path, "operstate")),
            wireless=False,
            virtual=virtual,
            driver=os.path.basename(os.path.realpath(os.path.join(device, "driver"))) if not virtual else "",
            bus="usb" if "/usb" in real else ("pci" if real else ""),
            speed=int(speed_raw) if speed_raw.lstrip("-").isdigit() else 0,
            ipv4=addrs.get(name, []),
        ))
    return nics


def list_nics_macos(include_virtual: bool = False) -> List[Nic]:
    from ..helper import os_macos as M

    ports = M.parse_hardware_ports(M.run([M.NETWORKSETUP, "-listallhardwareports"])[1])
    ifs = M.parse_ifconfig(M.run([M.IFCONFIG, "-a"])[1])
    nics = []
    for name, info in sorted(ifs.items()):
        if name.startswith("lo") or not info["mac"]:
            continue
        port = ports.get(name, "")
        kind = M.port_kind(port) if port else "virtual"
        if kind == "wifi":
            continue
        virtual = kind == "virtual"
        if virtual and not include_virtual:
            continue
        status = info["status"]
        nics.append(Nic(
            name=name, mac=info["mac"], carrier=None if not status else status == "active",
            operstate="up" if "UP" in info["flags"] else "down", virtual=virtual,
            bus="usb" if "usb" in port.lower() else ("pci" if port else ""),
            speed=M.media_speed(info["media"]), ipv4=list(info["ipv4"]), desc=port,
        ))
    return nics


def list_nics_windows(include_virtual: bool = False) -> List[Nic]:
    from ..helper import winnet

    nics = []
    for a in winnet.adapters():
        if a["type"] != winnet.IF_TYPE_ETHERNET_CSMACD or not a["mac"]:
            continue
        virtual = winnet.looks_virtual(a)
        if virtual and not include_virtual:
            continue
        nics.append(Nic(
            name=a["name"], mac=a["mac"], carrier=a["up"], operstate="up" if a["up"] else "down",
            virtual=virtual, speed=a["speed"] if a["up"] else 0,
            ipv4=[c for c in a["ipv4"] if not c.startswith("169.254.")], desc=a["description"],
        ))
    return nics


def list_nics_generic() -> List[Nic]:
    """其它系统：用 psutil 简单列出。"""
    try:
        import psutil
    except ImportError:
        return []
    stats = psutil.net_if_stats()
    result = []
    for name, addrs in psutil.net_if_addrs().items():
        mac = ""
        ipv4 = []
        for a in addrs:
            fam = getattr(a.family, "name", str(a.family))
            if fam in ("AF_LINK", "AF_PACKET"):
                mac = a.address.lower().replace("-", ":")
            elif fam == "AF_INET":
                ipv4.append(a.address)
        st = stats.get(name)
        result.append(Nic(name=name, mac=mac, carrier=bool(st and st.isup), ipv4=ipv4,
                          speed=st.speed if st else 0))
    return result


def list_nics(include_virtual: bool = False) -> List[Nic]:
    if sys.platform.startswith("linux"):
        return list_nics_linux(include_virtual)
    try:
        if sys.platform == "darwin":
            return list_nics_macos(include_virtual)
        if sys.platform == "win32":
            return list_nics_windows(include_virtual)
    except (OSError, ValueError):
        pass
    return list_nics_generic()


def pick_default(nics: List[Nic], preferred: str = "") -> Optional[Nic]:
    """优先：上次用的网卡 > 插了网线的物理网卡 > 第一块物理网卡。"""
    for n in nics:
        if n.name == preferred and n.carrier:
            return n
    for n in nics:
        if n.carrier and not n.virtual:
            return n
    for n in nics:
        if n.name == preferred:
            return n
    physical = [n for n in nics if not n.virtual]
    return (physical or nics or [None])[0]


def carrier(name: str) -> Optional[bool]:
    """网卡是否插了网线（不知道时返回 None）。"""
    if sys.platform == "darwin":
        from ..helper.os_macos import MacBackend

        return MacBackend().carrier(name)
    if sys.platform == "win32":
        from ..helper import winnet

        try:
            a = winnet.adapter(name)
        except OSError:
            return None
        return a["up"] if a else None
    raw = _read("/sys/class/net/%s/carrier" % name)
    return None if raw == "" else raw == "1"


def ipv4_by_iface() -> Dict[str, List[str]]:
    """本机各网卡的 IPv4 地址（CIDR），用来避开与本机网络冲突的直连网段。"""
    if sys.platform.startswith("linux"):
        return _ip_addresses()
    try:
        import psutil
    except ImportError:
        return {}
    result: Dict[str, List[str]] = {}
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family != socket.AF_INET or not a.netmask:
                continue
            try:
                iface = ipaddress.ip_interface("%s/%s" % (a.address, a.netmask))
            except ValueError:
                continue
            result.setdefault(name, []).append(str(iface))
    return result
