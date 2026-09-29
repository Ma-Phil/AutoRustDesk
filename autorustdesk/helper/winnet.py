"""Windows 系统接口（ctypes 调用 iphlpapi / shell32）：

- adapters()：网卡列表（名称、序号、GUID、MAC、类型、状态、地址、是否 DHCP）
- neighbors()：系统邻居表（ARP / IPv6 邻居缓存）
- send_arp()：用系统的 SendARP 解析一个地址的 MAC
- is_admin()、run_as_admin()：管理员权限与 UAC 提权

结构体按 Windows SDK 的定义逐字段写出，全部使用定长类型，便于在其它系统上检查布局。
"""

import ctypes
import socket
import struct
import sys
from ctypes import POINTER, Structure, c_char_p, c_int32, c_uint8, c_uint32, c_uint64, c_void_p, c_wchar_p
from typing import Dict, List, Optional

AF_UNSPEC = 0
AF_INET = 2
AF_INET6 = 23  # Windows 上的值

GAA_FLAG_SKIP_ANYCAST = 0x2
GAA_FLAG_SKIP_MULTICAST = 0x4
GAA_FLAG_SKIP_DNS_SERVER = 0x8
GAA_FLAG_INCLUDE_GATEWAYS = 0x80
ERROR_BUFFER_OVERFLOW = 111
ERROR_NO_DATA = 232
ERROR_CANCELLED = 1223

IF_TYPE_ETHERNET_CSMACD = 6
IF_TYPE_IEEE80211 = 71
IF_OPER_STATUS_UP = 1
IP_ADAPTER_DHCP_ENABLED = 0x4
IP_PREFIX_ORIGIN_DHCP = 3
# IP_DAD_STATE：地址刚配置时先是"暂定"（重复地址检测中），之后才能绑定使用
IP_DAD_STATE_DEPRECATED = 3
IP_DAD_STATE_PREFERRED = 4

# NL_NEIGHBOR_STATE
NLNS_UNREACHABLE, NLNS_INCOMPLETE, NLNS_PROBE, NLNS_DELAY, NLNS_STALE, NLNS_REACHABLE, NLNS_PERMANENT = range(7)


class SOCKET_ADDRESS(Structure):
    _fields_ = [("lpSockaddr", c_void_p), ("iSockaddrLength", c_int32)]


class IP_ADAPTER_UNICAST_ADDRESS(Structure):
    pass


IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", c_uint32),
    ("Flags", c_uint32),
    ("Next", POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", SOCKET_ADDRESS),
    ("PrefixOrigin", c_int32),
    ("SuffixOrigin", c_int32),
    ("DadState", c_int32),
    ("ValidLifetime", c_uint32),
    ("PreferredLifetime", c_uint32),
    ("LeaseLifetime", c_uint32),
    ("OnLinkPrefixLength", c_uint8),
]


class IP_ADAPTER_GATEWAY_ADDRESS(Structure):
    pass


IP_ADAPTER_GATEWAY_ADDRESS._fields_ = [
    ("Length", c_uint32),
    ("Reserved", c_uint32),
    ("Next", POINTER(IP_ADAPTER_GATEWAY_ADDRESS)),
    ("Address", SOCKET_ADDRESS),
]


class IP_ADAPTER_ADDRESSES(Structure):
    pass


IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", c_uint32),
    ("IfIndex", c_uint32),
    ("Next", POINTER(IP_ADAPTER_ADDRESSES)),
    ("AdapterName", c_char_p),
    ("FirstUnicastAddress", POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", c_void_p),
    ("FirstMulticastAddress", c_void_p),
    ("FirstDnsServerAddress", c_void_p),
    ("DnsSuffix", c_wchar_p),
    ("Description", c_wchar_p),
    ("FriendlyName", c_wchar_p),
    ("PhysicalAddress", c_uint8 * 8),
    ("PhysicalAddressLength", c_uint32),
    ("Flags", c_uint32),
    ("Mtu", c_uint32),
    ("IfType", c_uint32),
    ("OperStatus", c_int32),
    ("Ipv6IfIndex", c_uint32),
    ("ZoneIndices", c_uint32 * 16),
    ("FirstPrefix", c_void_p),
    ("TransmitLinkSpeed", c_uint64),
    ("ReceiveLinkSpeed", c_uint64),
    ("FirstWinsServerAddress", c_void_p),
    ("FirstGatewayAddress", POINTER(IP_ADAPTER_GATEWAY_ADDRESS)),
    ("Ipv4Metric", c_uint32),
    ("Ipv6Metric", c_uint32),
    ("Luid", c_uint64),
    ("Dhcpv4Server", SOCKET_ADDRESS),
    ("CompartmentId", c_uint32),
    ("NetworkGuid", c_uint8 * 16),
    ("ConnectionType", c_uint32),
    ("TunnelType", c_uint32),
    ("Dhcpv6Server", SOCKET_ADDRESS),
    ("Dhcpv6ClientDuid", c_uint8 * 130),
    ("Dhcpv6ClientDuidLength", c_uint32),
    ("Dhcpv6Iaid", c_uint32),
    ("FirstDnsSuffix", c_void_p),
]


class MIB_IPNET_ROW2(Structure):
    _fields_ = [
        ("Address", c_uint8 * 28),  # SOCKADDR_INET
        ("InterfaceIndex", c_uint32),
        ("InterfaceLuid", c_uint64),
        ("PhysicalAddress", c_uint8 * 32),
        ("PhysicalAddressLength", c_uint32),
        ("State", c_int32),
        ("Flags", c_uint8),
        ("ReachabilityTime", c_uint32),
    ]


class MIB_IPNET_TABLE2(Structure):
    _fields_ = [("NumEntries", c_uint32), ("Table", MIB_IPNET_ROW2 * 1)]


class SHELLEXECUTEINFOW(Structure):
    _fields_ = [
        ("cbSize", c_uint32),
        ("fMask", c_uint32),
        ("hwnd", c_void_p),
        ("lpVerb", c_wchar_p),
        ("lpFile", c_wchar_p),
        ("lpParameters", c_wchar_p),
        ("lpDirectory", c_wchar_p),
        ("nShow", c_int32),
        ("hInstApp", c_void_p),
        ("lpIDList", c_void_p),
        ("lpClass", c_wchar_p),
        ("hkeyClass", c_void_p),
        ("dwHotKey", c_uint32),
        ("hIconOrMonitor", c_void_p),
        ("hProcess", c_void_p),
    ]


# ---------------------------------------------------------------------------
# 解析（与系统调用分开，便于测试）
# ---------------------------------------------------------------------------


def sockaddr_ip(addr: int) -> Optional[str]:
    """读取内存中的 SOCKADDR（Windows 布局），返回地址字符串。"""
    if not addr:
        return None
    family = ctypes.c_uint16.from_address(addr).value
    if family == AF_INET:
        return socket.inet_ntoa(ctypes.string_at(addr + 4, 4))
    if family == AF_INET6:
        return socket.inet_ntop(socket.AF_INET6, ctypes.string_at(addr + 8, 16))
    return None


def sockaddr_inet_ip(raw: bytes) -> Optional[str]:
    """SOCKADDR_INET（邻居表里的地址）→ 地址字符串。"""
    family = struct.unpack_from("<H", raw, 0)[0]
    if family == AF_INET:
        return socket.inet_ntoa(raw[4:8])
    if family == AF_INET6:
        return socket.inet_ntop(socket.AF_INET6, raw[8:24])
    return None


def _mac(raw, length: int) -> str:
    return ":".join("%02x" % b for b in bytes(raw)[:length])


def adapter_info(a: IP_ADAPTER_ADDRESSES) -> Dict:
    info: Dict = {
        "name": a.FriendlyName or "",
        "description": a.Description or "",
        "guid": (a.AdapterName or b"").decode("ascii", "replace"),
        "index": a.IfIndex or a.Ipv6IfIndex,
        "ipv6_index": a.Ipv6IfIndex or a.IfIndex,
        "mac": _mac(a.PhysicalAddress, a.PhysicalAddressLength) if a.PhysicalAddressLength == 6 else "",
        "type": a.IfType,
        "up": a.OperStatus == IF_OPER_STATUS_UP,
        "dhcp": bool(a.Flags & IP_ADAPTER_DHCP_ENABLED),
        "speed": a.ReceiveLinkSpeed // 1000000 if 0 < a.ReceiveLinkSpeed < (1 << 62) else 0,
        "ipv4": [], "ipv4_ready": [], "ipv4_dhcp": [], "ipv6ll": [], "ipv6": [], "gateways": [],
        "dhcp_server": sockaddr_ip(a.Dhcpv4Server.lpSockaddr) or "",
    }
    u = a.FirstUnicastAddress
    while u:
        ua = u.contents
        ip = sockaddr_ip(ua.Address.lpSockaddr)
        if ip and ":" not in ip:
            cidr = "%s/%d" % (ip, ua.OnLinkPrefixLength)
            info["ipv4"].append(cidr)
            if ua.DadState in (IP_DAD_STATE_PREFERRED, IP_DAD_STATE_DEPRECATED):
                info["ipv4_ready"].append(cidr)
            if ua.PrefixOrigin == IP_PREFIX_ORIGIN_DHCP:
                info["ipv4_dhcp"].append(cidr)
        elif ip:
            ip = ip.split("%")[0]
            info["ipv6ll" if ip.lower().startswith("fe80:") else "ipv6"].append(ip)
        u = ua.Next
    g = a.FirstGatewayAddress
    while g:
        ip = sockaddr_ip(g.contents.Address.lpSockaddr)
        if ip:
            info["gateways"].append(ip)
        g = g.contents.Next
    return info


def neighbor_info(row: MIB_IPNET_ROW2) -> Optional[Dict]:
    ip = sockaddr_inet_ip(bytes(row.Address))
    if not ip or row.PhysicalAddressLength != 6:
        return None
    mac = _mac(row.PhysicalAddress, 6)
    if mac == "00:00:00:00:00:00":
        return None
    return {"ip": ip, "mac": mac, "ifindex": row.InterfaceIndex, "state": row.State}


VIRTUAL_WORDS = (
    "virtual", "hyper-v", "vethernet", "vmware", "virtualbox", "tap-", "tap adapter", "wintun",
    "wireguard", "tailscale", "zerotier", "openvpn", "vpn", "bluetooth", "loopback", "npcap",
    "kernel debug", "wan miniport", "teredo", "isatap", "docker", "wsl", "fortinet", "anyconnect",
)


def looks_virtual(info: Dict) -> bool:
    text = ("%s %s" % (info.get("name", ""), info.get("description", ""))).lower()
    return any(w in text for w in VIRTUAL_WORDS)


# ---------------------------------------------------------------------------
# 系统调用（只在 Windows 上可用）
# ---------------------------------------------------------------------------


def _iphlpapi():
    lib = ctypes.WinDLL("iphlpapi")  # type: ignore[attr-defined]
    lib.GetAdaptersAddresses.argtypes = [c_uint32, c_uint32, c_void_p, c_void_p, POINTER(c_uint32)]
    lib.GetAdaptersAddresses.restype = c_uint32
    lib.GetIpNetTable2.argtypes = [c_uint32, POINTER(POINTER(MIB_IPNET_TABLE2))]
    lib.GetIpNetTable2.restype = c_uint32
    lib.FreeMibTable.argtypes = [c_void_p]
    lib.FreeMibTable.restype = None
    lib.SendARP.argtypes = [c_uint32, c_uint32, c_void_p, POINTER(c_uint32)]
    lib.SendARP.restype = c_uint32
    return lib


def adapters() -> List[Dict]:
    lib = _iphlpapi()
    flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER | GAA_FLAG_INCLUDE_GATEWAYS
    size = c_uint32(32768)
    for _ in range(5):
        buf = ctypes.create_string_buffer(size.value)
        rc = lib.GetAdaptersAddresses(AF_UNSPEC, flags, None, buf, ctypes.byref(size))
        if rc == ERROR_BUFFER_OVERFLOW:
            continue
        if rc == ERROR_NO_DATA:
            return []
        if rc != 0:
            raise OSError(rc, "GetAdaptersAddresses 失败（%d）" % rc)
        result = []
        p = ctypes.cast(buf, POINTER(IP_ADAPTER_ADDRESSES))
        while p:
            result.append(adapter_info(p.contents))
            p = p.contents.Next
        return result
    raise OSError("GetAdaptersAddresses：缓冲区不够")


def adapter(name: str) -> Optional[Dict]:
    for a in adapters():
        if a["name"] == name:
            return a
    return None


def neighbors(ifindex: Optional[int] = None) -> List[Dict]:
    lib = _iphlpapi()
    table = POINTER(MIB_IPNET_TABLE2)()
    rc = lib.GetIpNetTable2(AF_UNSPEC, ctypes.byref(table))
    if rc != 0:
        return []
    try:
        n = table.contents.NumEntries
        base = ctypes.addressof(table.contents) + MIB_IPNET_TABLE2.Table.offset
        out = []
        for i in range(n):
            row = MIB_IPNET_ROW2.from_address(base + i * ctypes.sizeof(MIB_IPNET_ROW2))
            if ifindex is not None and row.InterfaceIndex != ifindex:
                continue
            if row.State < NLNS_PROBE:
                continue  # 无法到达 / 还没解析出来
            info = neighbor_info(row)
            if info:
                out.append(info)
        return out
    finally:
        lib.FreeMibTable(table)


def _ipaddr(ip: str) -> int:
    # IPAddr 是按网络字节序存放的 32 位数
    return struct.unpack("<I", socket.inet_aton(ip))[0]


def send_arp(target: str, sender: str = "0.0.0.0") -> Optional[str]:
    """发 ARP 请求并等待回应（阻塞，没有回应时约 1~3 秒后返回 None）。"""
    lib = _iphlpapi()
    buf = (c_uint8 * 8)()
    length = c_uint32(8)
    rc = lib.SendARP(_ipaddr(target), _ipaddr(sender), buf, ctypes.byref(length))
    if rc != 0 or length.value != 6:
        return None
    return _mac(buf, 6)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def oem_encoding() -> str:
    """命令行程序（netsh 等）输出所用的编码。"""
    try:
        return "cp%d" % ctypes.windll.kernel32.GetOEMCP()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return "utf-8"


SEE_MASK_NOCLOSEPROCESS = 0x40
SW_HIDE = 0
STILL_ACTIVE = 259


def run_as_admin(exe: str, params: str, cwd: Optional[str] = None) -> int:
    """以管理员身份启动程序（弹出 UAC 确认框），返回进程句柄。用户拒绝时抛出 PermissionError。"""
    ole32 = ctypes.WinDLL("ole32")  # type: ignore[attr-defined]
    ole32.CoInitializeEx(None, 0x2 | 0x4)  # COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]
    shell32.ShellExecuteExW.argtypes = [POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = c_int32
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params
    info.lpDirectory = cwd
    info.nShow = SW_HIDE
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()  # type: ignore[attr-defined]
        if err == ERROR_CANCELLED:
            raise PermissionError("用户取消了管理员授权")
        raise OSError(err, "以管理员身份启动失败：%s" % ctypes.FormatError(err))  # type: ignore[attr-defined]
    return info.hProcess or 0


def process_exit_code(handle: int) -> Optional[int]:
    """进程还在运行时返回 None。"""
    code = c_uint32()
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k32.GetExitCodeProcess.argtypes = [c_void_p, POINTER(c_uint32)]
    if not k32.GetExitCodeProcess(c_void_p(handle), ctypes.byref(code)):
        return None
    return None if code.value == STILL_ACTIVE else code.value


def close_handle(handle: int) -> None:
    if handle and sys.platform == "win32":
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        k32.CloseHandle.argtypes = [c_void_p]
        k32.CloseHandle(c_void_p(handle))
