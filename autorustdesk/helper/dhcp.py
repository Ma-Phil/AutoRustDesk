"""极简 DHCP 服务器：只给直连的电脑 B 分配地址。

刻意不下发网关（option 3）和 DNS（option 6），这样 B 不会把默认路由指向 A，
B 原有网络不受影响。
"""

import ipaddress
import socket
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

MAGIC = b"\x63\x82\x53\x63"

DISCOVER, OFFER, REQUEST, DECLINE, ACK, NAK, RELEASE, INFORM = range(1, 9)
MSG_NAMES = {
    DISCOVER: "DISCOVER", OFFER: "OFFER", REQUEST: "REQUEST", DECLINE: "DECLINE",
    ACK: "ACK", NAK: "NAK", RELEASE: "RELEASE", INFORM: "INFORM",
}

OPT_SUBNET_MASK = 1
OPT_ROUTER = 3
OPT_DNS = 6
OPT_HOSTNAME = 12
OPT_BROADCAST = 28
OPT_REQUESTED_IP = 50
OPT_LEASE_TIME = 51
OPT_MSG_TYPE = 53
OPT_SERVER_ID = 54
OPT_RENEWAL_T1 = 58
OPT_REBINDING_T2 = 59
OPT_VENDOR_CLASS = 60
OPT_CLIENT_ID = 61
OPT_END = 255
OPT_PAD = 0

EventFn = Callable[[Dict], None]
SocketFactory = Callable[[], socket.socket]


def mac_str(raw: bytes) -> str:
    return ":".join("%02x" % b for b in raw[:6])


def mac_bytes(mac: str) -> bytes:
    return bytes(int(x, 16) for x in mac.split(":"))


class DhcpPacket:
    """BOOTP/DHCP 报文。"""

    def __init__(self) -> None:
        self.op = 1
        self.htype = 1
        self.hlen = 6
        self.hops = 0
        self.xid = 0
        self.secs = 0
        self.flags = 0
        self.ciaddr = "0.0.0.0"
        self.yiaddr = "0.0.0.0"
        self.siaddr = "0.0.0.0"
        self.giaddr = "0.0.0.0"
        self.chaddr = b"\x00" * 16
        self.options: Dict[int, bytes] = {}

    @classmethod
    def parse(cls, data: bytes) -> "DhcpPacket":
        if len(data) < 240 or data[236:240] != MAGIC:
            raise ValueError("不是 DHCP 报文")
        p = cls()
        (p.op, p.htype, p.hlen, p.hops, p.xid, p.secs, p.flags) = struct.unpack("!BBBBIHH", data[:12])
        p.ciaddr = socket.inet_ntoa(data[12:16])
        p.yiaddr = socket.inet_ntoa(data[16:20])
        p.siaddr = socket.inet_ntoa(data[20:24])
        p.giaddr = socket.inet_ntoa(data[24:28])
        p.chaddr = data[28:44]
        i = 240
        while i < len(data):
            code = data[i]
            if code == OPT_END:
                break
            if code == OPT_PAD:
                i += 1
                continue
            if i + 1 >= len(data):
                break
            length = data[i + 1]
            value = data[i + 2:i + 2 + length]
            # 同一选项出现多次时按 RFC 3396 拼接
            p.options[code] = p.options.get(code, b"") + value
            i += 2 + length
        return p

    def build(self) -> bytes:
        head = struct.pack(
            "!BBBBIHH", self.op, self.htype, self.hlen, self.hops, self.xid, self.secs, self.flags
        )
        head += socket.inet_aton(self.ciaddr) + socket.inet_aton(self.yiaddr)
        head += socket.inet_aton(self.siaddr) + socket.inet_aton(self.giaddr)
        head += self.chaddr.ljust(16, b"\x00")[:16]
        head += b"\x00" * 192  # sname + file
        opts = b""
        for code, value in self.options.items():
            for k in range(0, max(len(value), 1), 255):
                chunk = value[k:k + 255]
                opts += bytes([code, len(chunk)]) + chunk
        data = head + MAGIC + opts + bytes([OPT_END])
        if len(data) < 300:
            data += b"\x00" * (300 - len(data))
        return data

    @property
    def mac(self) -> str:
        return mac_str(self.chaddr)

    @property
    def msg_type(self) -> int:
        v = self.options.get(OPT_MSG_TYPE)
        return v[0] if v else 0

    def opt_ip(self, code: int) -> Optional[str]:
        v = self.options.get(code)
        if v and len(v) == 4:
            return socket.inet_ntoa(v)
        return None

    def opt_text(self, code: int) -> str:
        v = self.options.get(code)
        return v.decode("utf-8", "replace").strip("\x00") if v else ""


class Lease:
    def __init__(self, mac: str, ip: str, expires: float, hostname: str = "", offered: bool = True):
        self.mac = mac
        self.ip = ip
        self.expires = expires
        self.hostname = hostname
        self.offered = offered  # True 表示只 OFFER 过，还没 ACK


class DhcpServer:
    """在一块网卡上运行的 DHCP 服务。handle() 是纯逻辑，便于测试。"""

    def __init__(
        self,
        iface: str,
        server_ip: str,
        prefix: int,
        pool_start: str,
        pool_end: str,
        lease_time: int = 3600,
        reservations: Optional[Dict[str, str]] = None,
        on_event: Optional[EventFn] = None,
        socket_factory: Optional[SocketFactory] = None,
        subnet_broadcast: bool = False,
    ) -> None:
        self.iface = iface
        # 各系统把套接字限定在直连网卡上的方法不同，由 backend 提供；默认用 Linux 的做法
        self.socket_factory = socket_factory or self._linux_socket
        # 广播回复发往 255.255.255.255；macOS 上绑定网卡的套接字发不出这个地址（没有路由），
        # 改发本网段广播地址（在网线上同样是以太网广播，B 一样能收到）
        self.subnet_broadcast = subnet_broadcast
        self.server_ip = server_ip
        self.network = ipaddress.ip_network("%s/%d" % (server_ip, prefix), strict=False)
        self.pool_start = ipaddress.ip_address(pool_start)
        self.pool_end = ipaddress.ip_address(pool_end)
        if self.pool_start not in self.network or self.pool_end not in self.network:
            raise ValueError("地址池不在网段 %s 内" % self.network)
        if self.pool_start > self.pool_end:
            raise ValueError("地址池起止颠倒")
        self.lease_time = int(lease_time)
        self.reservations = {k.lower(): v for k, v in (reservations or {}).items()}
        self.on_event = on_event or (lambda e: None)
        self.leases: Dict[str, Lease] = {}
        self.declined: Dict[str, float] = {}
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 分配

    def _in_pool(self, ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return self.pool_start <= a <= self.pool_end and str(a) != self.server_ip

    def _taken_by_other(self, ip: str, mac: str, now: float) -> bool:
        for m, lease in self.leases.items():
            if m != mac and lease.ip == ip and lease.expires > now:
                return True
        for m, rip in self.reservations.items():
            if m != mac and rip == ip:
                return True
        return self.declined.get(ip, 0) > now

    def _usable(self, ip: Optional[str], mac: str, now: float) -> bool:
        if not ip:
            return False
        if ip in self.reservations.values() and self.reservations.get(mac) != ip:
            return False
        try:
            in_net = ipaddress.ip_address(ip) in self.network
        except ValueError:
            return False
        if not in_net or ip == self.server_ip:
            return False
        return not self._taken_by_other(ip, mac, now)

    def _allocate(self, mac: str, requested: Optional[str], now: float) -> Optional[str]:
        reserved = self.reservations.get(mac)
        if reserved and self._usable(reserved, mac, now):
            return reserved
        lease = self.leases.get(mac)
        if lease and self._usable(lease.ip, mac, now):
            return lease.ip
        if requested and self._in_pool(requested) and self._usable(requested, mac, now):
            return requested
        a = self.pool_start
        while a <= self.pool_end:
            ip = str(a)
            if self._in_pool(ip) and self._usable(ip, mac, now):
                return ip
            a += 1
        return None

    # ------------------------------------------------------------------ 报文处理

    def _reply(self, req: DhcpPacket, msg_type: int, yiaddr: str) -> DhcpPacket:
        r = DhcpPacket()
        r.op = 2
        r.htype, r.hlen = req.htype, req.hlen
        r.xid = req.xid
        r.flags = req.flags
        r.ciaddr = req.ciaddr if msg_type == ACK else "0.0.0.0"
        r.yiaddr = yiaddr
        r.giaddr = req.giaddr
        r.chaddr = req.chaddr
        r.options[OPT_MSG_TYPE] = bytes([msg_type])
        r.options[OPT_SERVER_ID] = socket.inet_aton(self.server_ip)
        if msg_type in (OFFER, ACK):
            r.options[OPT_SUBNET_MASK] = socket.inet_aton(str(self.network.netmask))
            r.options[OPT_BROADCAST] = socket.inet_aton(str(self.network.broadcast_address))
            if yiaddr != "0.0.0.0":
                lt = self.lease_time
                r.options[OPT_LEASE_TIME] = struct.pack("!I", lt)
                r.options[OPT_RENEWAL_T1] = struct.pack("!I", lt // 2)
                r.options[OPT_REBINDING_T2] = struct.pack("!I", lt * 7 // 8)
        return r

    def handle(self, req: DhcpPacket, now: Optional[float] = None) -> Optional[Tuple[DhcpPacket, str]]:
        """处理一个请求，返回 (回复, 目的地址) 或 None。"""
        if req.op != 1 or req.htype != 1 or req.hlen != 6:
            return None
        now = time.time() if now is None else now
        mac = req.mac
        mtype = req.msg_type
        hostname = req.opt_text(OPT_HOSTNAME)
        server_id = req.opt_ip(OPT_SERVER_ID)
        requested = req.opt_ip(OPT_REQUESTED_IP)

        with self._lock:
            if mtype == DISCOVER:
                self.on_event({"event": "dhcp_discover", "mac": mac, "hostname": hostname,
                               "vendor": req.opt_text(OPT_VENDOR_CLASS)})
                ip = self._allocate(mac, requested, now)
                if not ip:
                    self.on_event({"event": "log", "level": "warning", "msg": "DHCP 地址池已满"})
                    return None
                self.leases[mac] = Lease(mac, ip, now + 60, hostname, offered=True)
                return self._reply(req, OFFER, ip), "255.255.255.255"

            if mtype == REQUEST:
                if server_id and server_id != self.server_ip:
                    # 客户端选择了别的 DHCP 服务器
                    self.leases.pop(mac, None)
                    return None
                wanted = requested or (req.ciaddr if req.ciaddr != "0.0.0.0" else None)
                ip = self._allocate(mac, wanted, now)
                if not wanted or ip != wanted:
                    return self._reply(req, NAK, "0.0.0.0"), "255.255.255.255"
                self.leases[mac] = Lease(mac, ip, now + self.lease_time, hostname, offered=False)
                self.on_event({"event": "lease", "mac": mac, "ip": ip, "hostname": hostname,
                               "vendor": req.opt_text(OPT_VENDOR_CLASS),
                               "prefix": self.network.prefixlen})
                dest = req.ciaddr if req.ciaddr != "0.0.0.0" else "255.255.255.255"
                return self._reply(req, ACK, ip), dest

            if mtype == DECLINE:
                if requested:
                    self.declined[requested] = now + 600
                    self.on_event({"event": "log", "level": "warning",
                                   "msg": "%s 拒绝了地址 %s（地址冲突）" % (mac, requested)})
                self.leases.pop(mac, None)
                return None

            if mtype == RELEASE:
                lease = self.leases.get(mac)
                if lease and lease.ip == req.ciaddr:
                    lease.expires = now
                return None

            if mtype == INFORM:
                dest = req.ciaddr if req.ciaddr != "0.0.0.0" else "255.255.255.255"
                return self._reply(req, ACK, "0.0.0.0"), dest
        return None

    # ------------------------------------------------------------------ 网络

    def _linux_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if hasattr(socket, "SO_BINDTODEVICE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.iface.encode() + b"\x00")
            s.bind(("0.0.0.0", 67))
        except OSError:
            s.close()
            raise
        return s

    def start(self) -> None:
        try:
            s = self.socket_factory()
        except OSError as e:
            raise OSError("无法监听 DHCP 端口 67（可能有别的 DHCP 服务在运行）：%s" % e)
        s.settimeout(0.5)
        self._sock = s
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dhcp-%s" % self.iface, daemon=True)
        self._thread.start()

    def restart(self, timeout: float = 15.0) -> None:
        """重新打开套接字（网卡被停用再启用后，绑定在网卡地址上的套接字会失效）。已分配的地址保留。"""
        self.stop()
        deadline = time.time() + timeout
        while True:
            try:
                self.start()
                return
            except OSError:
                # 网卡刚启用时地址还没生效（重复地址检测），稍等再试
                if time.time() >= deadline:
                    raise
                time.sleep(0.5)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._sock:
            self._sock.close()
        self._sock = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(0.2)
                continue
            try:
                req = DhcpPacket.parse(data)
            except ValueError:
                continue
            try:
                out = self.handle(req)
            except Exception as e:  # noqa: BLE001 - 不让一个坏报文打断服务
                self.on_event({"event": "log", "level": "error", "msg": "DHCP 处理出错：%s" % e})
                continue
            if not out:
                continue
            reply, dest = out
            try:
                self._send(reply.build(), dest)
                self.on_event({"event": "log", "level": "debug", "msg": "DHCP %s %s → %s" % (
                    MSG_NAMES.get(reply.msg_type, "?"), reply.yiaddr, reply.mac)})
            except OSError as e:
                self.on_event({"event": "log", "level": "error", "msg": "DHCP 发送失败：%s" % e})

    def _send(self, data: bytes, dest: str) -> None:
        assert self._sock is not None
        subnet_bcast = str(self.network.broadcast_address)
        if dest == "255.255.255.255" and self.subnet_broadcast:
            dest = subnet_bcast
        try:
            self._sock.sendto(data, (dest, 68))
        except OSError:
            # 一种广播地址发不出去时换另一种
            if dest == "255.255.255.255":
                self._sock.sendto(data, (subnet_bcast, 68))
                self.subnet_broadcast = True
            elif dest == subnet_bcast:
                self._sock.sendto(data, ("255.255.255.255", 68))
                self.subnet_broadcast = False
            else:
                raise

    def snapshot(self) -> List[Dict]:
        now = time.time()
        return [
            {"mac": lease.mac, "ip": lease.ip, "hostname": lease.hostname,
             "expires_in": int(lease.expires - now), "offered": lease.offered}
            for lease in self.leases.values()
        ]
