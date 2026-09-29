"""在一块网卡上收发以太网帧（抓包、发 ARP）。

- Linux：AF_PACKET 原始套接字，内核自带。
- macOS：系统自带的 libpcap（/usr/lib/libpcap.A.dylib）。
- Windows：Npcap 提供的 wpcap.dll，需要用户自己安装 Npcap。

libpcap 用 ctypes 调用，不需要编译任何东西；Linux 上装了 libpcap 时也能用它（便于测试）。
"""

import ctypes
import ctypes.util
import locale
import os
import select
import socket
import sys
import threading
import time
from typing import List, Optional

ETH_P_ALL = 0x0003
PACKET_OUTGOING = 4
MIN_FRAME = 60  # 以太网最短帧（不含校验），短帧在末尾补零
PCAP_ERRBUF_SIZE = 256
DLT_EN10MB = 1
PCAP_D_IN = 1


class PacketIOError(Exception):
    pass


class PacketIO:
    """一块网卡上的原始以太网收发。recv 超时返回 None。"""

    kind = ""

    def recv(self, timeout: float) -> Optional[bytes]:
        raise NotImplementedError

    def send(self, frame: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> "PacketIO":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Linux AF_PACKET
# ---------------------------------------------------------------------------


class AfPacketIO(PacketIO):
    kind = "afpacket"

    def __init__(self, iface: str, send_only: bool = False):
        # 协议号为 0 的套接字收不到任何报文，只用来发送
        proto = 0 if send_only else ETH_P_ALL
        try:
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(proto))
            s.bind((iface, proto))
        except (OSError, AttributeError) as e:
            raise PacketIOError("无法在 %s 上收发原始报文：%s" % (iface, e))
        self.sock = s

    def recv(self, timeout: float) -> Optional[bytes]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                frame, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                return None
            except OSError as e:
                raise PacketIOError(str(e))
            if len(addr) > 2 and addr[2] == PACKET_OUTGOING:
                continue  # 本机发出的报文
            return frame

    def send(self, frame: bytes) -> None:
        try:
            self.sock.send(frame)
        except OSError as e:
            raise PacketIOError(str(e))

    def close(self) -> None:
        self.sock.close()


# ---------------------------------------------------------------------------
# libpcap（macOS 自带；Windows 为 Npcap）
# ---------------------------------------------------------------------------


class _Timeval(ctypes.Structure):
    # Linux/macOS 上 long 是 8 字节，Windows 上是 4 字节，与各自的 struct timeval 一致
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _PcapPkthdr(ctypes.Structure):
    # macOS 的 libpcap 在结构末尾还有 comment 字段；这里只通过指针读取开头部分，不受影响
    _fields_ = [("ts", _Timeval), ("caplen", ctypes.c_uint32), ("len", ctypes.c_uint32)]


_lib = None
_lib_tried = False
_lib_error = ""
_dll_dirs: List[object] = []  # os.add_dll_directory 的返回值，需要一直保留


def npcap_dir() -> str:
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "Npcap")


def _candidates() -> List[str]:
    if sys.platform == "win32":
        return [os.path.join(npcap_dir(), "wpcap.dll"), "wpcap.dll"]
    if sys.platform == "darwin":
        return ["/usr/lib/libpcap.A.dylib", "libpcap.dylib"]
    found = ctypes.util.find_library("pcap")
    return [n for n in (found, "libpcap.so.1", "libpcap.so.0.8", "libpcap.so") if n]


def _declare(lib) -> None:
    vp, ci, cp = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    lib.pcap_create.argtypes = [cp, cp]
    lib.pcap_create.restype = vp
    for name in ("pcap_set_snaplen", "pcap_set_promisc", "pcap_set_timeout",
                 "pcap_set_immediate_mode", "pcap_setdirection"):
        fn = getattr(lib, name, None)
        if fn is not None:
            fn.argtypes = [vp, ci]
            fn.restype = ci
    lib.pcap_activate.argtypes = [vp]
    lib.pcap_activate.restype = ci
    lib.pcap_geterr.argtypes = [vp]
    lib.pcap_geterr.restype = cp
    lib.pcap_datalink.argtypes = [vp]
    lib.pcap_datalink.restype = ci
    lib.pcap_setnonblock.argtypes = [vp, ci, cp]
    lib.pcap_setnonblock.restype = ci
    lib.pcap_next_ex.argtypes = [vp, ctypes.POINTER(ctypes.POINTER(_PcapPkthdr)),
                                 ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
    lib.pcap_next_ex.restype = ci
    lib.pcap_sendpacket.argtypes = [vp, vp, ci]
    lib.pcap_sendpacket.restype = ci
    lib.pcap_close.argtypes = [vp]
    lib.pcap_close.restype = None
    lib.pcap_lib_version.argtypes = []
    lib.pcap_lib_version.restype = cp
    fn = getattr(lib, "pcap_get_selectable_fd", None)
    if fn is not None:
        fn.argtypes = [vp]
        fn.restype = ci


def libpcap():
    """加载 libpcap；找不到时返回 None（原因见 libpcap_error()）。"""
    global _lib, _lib_tried, _lib_error
    if _lib is not None or _lib_tried:
        return _lib
    _lib_tried = True
    errors = []
    for name in _candidates():
        try:
            if sys.platform == "win32" and os.path.isabs(name):
                if not os.path.isfile(name):
                    errors.append("%s 不存在" % name)
                    continue
                if hasattr(os, "add_dll_directory"):
                    _dll_dirs.append(os.add_dll_directory(os.path.dirname(name)))
            lib = ctypes.CDLL(name)
            _declare(lib)
        except (OSError, AttributeError) as e:
            errors.append("%s：%s" % (name, e))
            continue
        _lib = lib
        return lib
    _lib_error = "；".join(errors) or "没有找到 libpcap"
    return None


def libpcap_error() -> str:
    return _lib_error


def libpcap_version() -> str:
    lib = libpcap()
    return _text(lib.pcap_lib_version()) if lib is not None else ""


def _text(raw: Optional[bytes]) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False) or "latin-1", "replace")


class PcapIO(PacketIO):
    """用 libpcap 打开一块网卡。device：macOS/Linux 为网卡名，Windows 为 \\Device\\NPF_{GUID}。"""

    kind = "pcap"

    def __init__(self, device: str, send_only: bool = False):
        lib = libpcap()
        if lib is None:
            hint = "请安装 Npcap（https://npcap.com）" if sys.platform == "win32" else libpcap_error()
            raise PacketIOError("无法抓包：没有找到 libpcap。%s" % hint)
        self.lib = lib
        self.device = device
        err = ctypes.create_string_buffer(PCAP_ERRBUF_SIZE)
        p = lib.pcap_create(device.encode("utf-8"), err)
        if not p:
            raise PacketIOError("打开网卡 %s 失败：%s" % (device, _text(err.value)))
        lib.pcap_set_snaplen(p, 128 if send_only else 65535)
        lib.pcap_set_promisc(p, 0)
        lib.pcap_set_timeout(p, 100)
        if getattr(lib, "pcap_set_immediate_mode", None) is not None:
            lib.pcap_set_immediate_mode(p, 1)
        rc = lib.pcap_activate(p)
        if rc < 0:
            msg = _text(lib.pcap_geterr(p)) or "错误码 %d" % rc
            lib.pcap_close(p)
            raise PacketIOError("打开网卡 %s 失败：%s" % (device, msg))
        if lib.pcap_datalink(p) != DLT_EN10MB:
            lib.pcap_close(p)
            raise PacketIOError("%s 不是以太网卡" % device)
        self._p = p
        self._lock = threading.Lock()
        lib.pcap_setnonblock(p, 1, err)
        setdir = getattr(lib, "pcap_setdirection", None)
        if setdir is not None:
            setdir(p, PCAP_D_IN)  # 不看自己发出的报文；不支持时忽略（邻居表会排除自己的 MAC）
        self.fd = -1
        getfd = getattr(lib, "pcap_get_selectable_fd", None)
        if getfd is not None and sys.platform != "win32":
            self.fd = getfd(p)
        self._hdr = ctypes.POINTER(_PcapPkthdr)()
        self._data = ctypes.POINTER(ctypes.c_ubyte)()

    def recv(self, timeout: float) -> Optional[bytes]:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if not self._p:
                    raise PacketIOError("抓包已关闭")
                rc = self.lib.pcap_next_ex(self._p, ctypes.byref(self._hdr), ctypes.byref(self._data))
                if rc == 1:
                    return ctypes.string_at(self._data, self._hdr.contents.caplen)
                if rc < 0:
                    raise PacketIOError(_text(self.lib.pcap_geterr(self._p)) or "抓包出错（%d）" % rc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if self.fd >= 0:
                try:
                    select.select([self.fd], [], [], min(remaining, 0.2))
                    continue
                except (OSError, ValueError):
                    pass
            time.sleep(min(remaining, 0.02))

    def send(self, frame: bytes) -> None:
        if len(frame) < MIN_FRAME:
            frame += b"\x00" * (MIN_FRAME - len(frame))
        buf = ctypes.create_string_buffer(frame, len(frame))
        with self._lock:
            if not self._p:
                raise PacketIOError("抓包已关闭")
            if self.lib.pcap_sendpacket(self._p, buf, len(frame)) != 0:
                raise PacketIOError(_text(self.lib.pcap_geterr(self._p)) or "发送失败")

    def close(self) -> None:
        with self._lock:
            if self._p:
                self.lib.pcap_close(self._p)
                self._p = None
