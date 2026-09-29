"""被动监听直连链路，找出对端设备的 MAC 和地址。

B 插上网线后通常会发出 ARP、DHCP、IPv6 邻居发现、mDNS 等报文，
从源地址就能知道 B 的 MAC、固定 IPv4、IPv6 链路本地地址和主机名。

抓包方式见 packetio（Linux AF_PACKET，macOS/Windows libpcap）。Windows 没装 Npcap
时无法抓包，改为定时读取系统的邻居表（NeighborPoller）。
"""

import ipaddress
import socket
import struct
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Set

from .packetio import PacketIO, PacketIOError

ETH_ARP = 0x0806
ETH_IPV4 = 0x0800
ETH_IPV6 = 0x86DD
ETH_VLAN = 0x8100

EventFn = Callable[[Dict], None]


def _mac(raw: bytes) -> str:
    return ":".join("%02x" % b for b in raw)


def _dhcp_hostname(payload: bytes) -> str:
    if len(payload) < 240 or payload[236:240] != b"\x63\x82\x53\x63":
        return ""
    i = 240
    while i + 1 < len(payload):
        code = payload[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        length = payload[i + 1]
        if code == 12:
            return payload[i + 2:i + 2 + length].decode("utf-8", "replace").strip("\x00")
        i += 2 + length
    return ""


def parse_frame(frame: bytes) -> Optional[Dict]:
    """解析以太网帧，返回源 MAC 及能识别出的地址信息。"""
    if len(frame) < 14:
        return None
    src = _mac(frame[6:12])
    etype = struct.unpack("!H", frame[12:14])[0]
    off = 14
    if etype == ETH_VLAN and len(frame) >= 18:
        etype = struct.unpack("!H", frame[16:18])[0]
        off = 18
    info: Dict = {"mac": src, "ethertype": etype}
    body = frame[off:]
    if etype == ETH_ARP and len(body) >= 28:
        htype, ptype, hlen, plen = struct.unpack("!HHBB", body[:6])
        if htype == 1 and ptype == ETH_IPV4 and hlen == 6 and plen == 4:
            spa = socket.inet_ntoa(body[14:18])
            if spa != "0.0.0.0":
                info["ipv4"] = spa
    elif etype == ETH_IPV4 and len(body) >= 20:
        ihl = (body[0] & 0x0F) * 4
        proto = body[9]
        src_ip = socket.inet_ntoa(body[12:16])
        if src_ip != "0.0.0.0" and not ipaddress.ip_address(src_ip).is_multicast:
            info["ipv4"] = src_ip
        if proto == 17 and len(body) >= ihl + 8:
            sport, dport = struct.unpack("!HH", body[ihl:ihl + 4])
            payload = body[ihl + 8:]
            if sport == 67 and dport == 68:
                info["dhcp_server"] = True
            elif sport == 68 and dport == 67:
                info["dhcp_client"] = True
                name = _dhcp_hostname(payload)
                if name:
                    info["hostname"] = name
    elif etype == ETH_IPV6 and len(body) >= 40:
        src6 = ipaddress.IPv6Address(body[8:24])
        if src6.is_link_local:
            info["ipv6ll"] = str(src6)
        elif not src6.is_unspecified and not src6.is_multicast:
            info["ipv6"] = str(src6)
    return info


class Neighbor:
    def __init__(self, mac: str):
        self.mac = mac
        self.ipv4: Set[str] = set()
        self.ipv6ll: Set[str] = set()
        self.ipv6: Set[str] = set()
        self.hostnames: Set[str] = set()
        self.dhcp_server = False
        self.dhcp_client = False
        self.first_seen = time.time()
        self.last_seen = self.first_seen
        self.frames = 0

    def to_dict(self) -> Dict:
        return {
            "mac": self.mac,
            "ipv4": sorted(self.ipv4),
            "ipv6ll": sorted(self.ipv6ll),
            "ipv6": sorted(self.ipv6),
            "hostnames": sorted(self.hostnames),
            "dhcp_server": self.dhcp_server,
            "dhcp_client": self.dhcp_client,
            "frames": self.frames,
            "age": round(time.time() - self.first_seen, 1),
        }


class NeighborTable:
    """汇总监听到的信息；信息有变化时回调。与抓包分离，便于测试。"""

    def __init__(self, own_macs: Iterable[str], own_ips: Iterable[str], on_event: EventFn):
        self.own_macs = {m.lower() for m in own_macs}
        self.own_ips = set(own_ips)
        self.on_event = on_event
        self.neighbors: Dict[str, Neighbor] = {}
        self._lock = threading.Lock()

    def feed(self, info: Dict) -> None:
        mac = info["mac"].lower()
        if mac in self.own_macs or mac == "ff:ff:ff:ff:ff:ff" or int(mac[:2], 16) & 1:
            return
        with self._lock:
            n = self.neighbors.get(mac)
            changed = False
            if n is None:
                n = self.neighbors[mac] = Neighbor(mac)
                changed = True
            n.frames += 1
            n.last_seen = time.time()
            for key, target in (("ipv4", n.ipv4), ("ipv6ll", n.ipv6ll), ("ipv6", n.ipv6),
                                ("hostname", n.hostnames)):
                v = info.get(key)
                if v and v not in self.own_ips and v not in target:
                    target.add(v)
                    changed = True
            if info.get("dhcp_server") and not n.dhcp_server:
                n.dhcp_server = True
                changed = True
                self.on_event({"event": "foreign_dhcp", "mac": mac})
            if info.get("dhcp_client") and not n.dhcp_client:
                n.dhcp_client = True
                changed = True
            if changed:
                self.on_event(dict(n.to_dict(), event="neighbor"))

    def snapshot(self) -> List[Dict]:
        with self._lock:
            return [n.to_dict() for n in self.neighbors.values()]


class LinkSniffer:
    """在后台线程里抓包并交给 NeighborTable。抓包出错（例如网卡被停用又启用）时自动重新打开。"""

    def __init__(self, open_io: Callable[[], PacketIO], table: NeighborTable, name: str = ""):
        self.open_io = open_io
        self.table = table
        self.name = name
        self.kind = ""
        self._io: Optional[PacketIO] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        """打开抓包（失败时抛出 PacketIOError）并开始监听。"""
        self._io = self.open_io()
        self.kind = self._io.kind
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sniff-%s" % self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._io:
            self._io.close()
        self._io = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        errors = 0
        while not self._stop.is_set():
            io = self._io
            if io is None:
                try:
                    io = self._io = self.open_io()
                except PacketIOError:
                    self._stop.wait(1.0)
                    continue
            try:
                frame = io.recv(0.5)
            except PacketIOError:
                if self._stop.is_set():
                    break
                errors += 1
                if errors >= 3:
                    # 连续出错：网卡可能被重置过，关掉重新打开
                    io.close()
                    self._io = None
                    errors = 0
                self._stop.wait(0.3)
                continue
            errors = 0
            if frame:
                info = parse_frame(frame)
                if info:
                    self.table.feed(info)


class NeighborPoller:
    """不能抓包时（Windows 没装 Npcap）的替代：定时读取系统邻居表（ARP/NDP 缓存）。

    B 通过 DHCP 拿地址、回应 IPv6 探测或者被 SendARP 探测到后，都会出现在邻居表里。
    list_fn 返回 [{"mac": ..., "ip": ...}, ...]。
    """

    kind = "neighbor-table"

    def __init__(self, list_fn: Callable[[], List[Dict]], table: NeighborTable, interval: float = 1.0):
        self.list_fn = list_fn
        self.table = table
        self.interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="neighbors", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def poll_once(self) -> None:
        for entry in self.list_fn():
            info: Dict = {"mac": entry["mac"]}
            try:
                ip = ipaddress.ip_address(entry["ip"].split("%")[0])
            except ValueError:
                continue
            if ip.is_multicast or ip.is_unspecified:
                continue
            if ip.version == 4:
                info["ipv4"] = str(ip)
            elif ip.is_link_local:
                info["ipv6ll"] = str(ip)
            else:
                info["ipv6"] = str(ip)
            self.table.feed(info)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - 读邻居表失败时下次再试
                pass
            self._stop.wait(self.interval)
