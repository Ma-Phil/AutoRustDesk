"""网络助手主循环：读取 JSON 请求，输出 JSON 响应和事件（每行一个）。

请求：{"id": 1, "cmd": "link_up", "iface": "enp3s0", "cidr": "192.168.77.1/24"}
响应：{"type": "response", "id": 1, "ok": true, "result": {...}}
事件：{"type": "event", "event": "lease", ...}
日志：{"type": "log", "level": "info", "msg": "..."}

Linux 上通过标准输入输出通信；macOS/Windows 上连回界面监听的本机端口（见 transport.py）。
连接断开（界面退出或崩溃）时自动恢复网卡并退出。
"""

import concurrent.futures
import importlib
import ipaddress
import json
import os
import signal
import sys
import threading
import traceback
from typing import Any, Callable, Dict, List, Optional, Set

from .. import __version__
from . import HELPER_PROTOCOL
from .arp import COMMON_SUBNETS, arp_scan, expand_targets, sender_for
from .backend import Backend, Link
from .backend import current as current_backend
from .dhcp import DhcpServer
from .icmp6 import probe_all_nodes
from .packetio import PacketIOError
from .sniffer import LinkSniffer, NeighborPoller, NeighborTable
from .state import STATE_DIR
from .transport import TransportError, connect_back, parse_args


class HelperError(Exception):
    pass


def _link_errors():
    """各平台 Link 实现抛出的配置错误类型（按需导入，避免在别的系统上导入失败）。"""
    errors = []
    for mod in ("linkconfig", "os_macos", "os_windows"):
        try:
            m = importlib.import_module("." + mod, __package__)
        except ImportError:
            continue
        errors.append(m.LinkError)
    return tuple(errors)


class Helper:
    def __init__(self, out=None, backend: Optional[Backend] = None) -> None:
        self.out = out or sys.stdout
        self.backend = backend or current_backend()
        self._link_errors = _link_errors()
        self._out_lock = threading.Lock()
        self.links: Dict[str, Link] = {}
        self.dhcp: Optional[DhcpServer] = None
        self.sniffer: Optional[Any] = None  # LinkSniffer 或 NeighborPoller
        self.table: Optional[NeighborTable] = None
        self.foreign_dhcp = False
        self._arp_busy = threading.Lock()
        self._shutdown = threading.Event()
        self._stop_watch = threading.Event()
        self._watcher: Optional[threading.Thread] = None
        # 正在做"电子拔插"的网卡：期间的断线/接上是我们自己造成的，不上报
        self._bouncing: Set[str] = set()

    # ------------------------------------------------------------------ 输出

    def send(self, obj: Dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with self._out_lock:
            try:
                self.out.write(line + "\n")
                self.out.flush()
            except (OSError, ValueError):  # 界面已断开
                self._shutdown.set()

    def log(self, msg: str, level: str = "info") -> None:
        self.send({"type": "log", "level": level, "msg": msg})

    def event(self, ev: Dict[str, Any]) -> None:
        if ev.get("event") == "log":
            self.log(ev.get("msg", ""), ev.get("level", "info"))
            return
        if ev.get("event") == "foreign_dhcp":
            self.foreign_dhcp = True
        self.send(dict(ev, type="event"))

    # ------------------------------------------------------------------ 命令

    def _iface(self, req: Dict) -> str:
        iface = req.get("iface", "")
        if not isinstance(iface, str) or not self.backend.iface_exists(iface):
            raise HelperError("网卡不存在：%r" % iface)
        return iface

    def cmd_hello(self, req: Dict) -> Dict:
        return {"version": __version__, "protocol": HELPER_PROTOCOL, "admin": self.backend.is_admin(),
                "pid": os.getpid(), "platform": sys.platform, "backend": self.backend.name}

    def cmd_link_up(self, req: Dict) -> Dict:
        iface = self._iface(req)
        if iface in self.links:
            self.links[iface].down()
        link = self.backend.link(iface, self.log)
        result = link.up(req["cidr"])
        self.links[iface] = link
        self._start_watcher()
        result["scope"] = self.backend.scope_id(iface)
        result["capture"] = self._capture_kind(iface)
        return result

    def _capture_kind(self, iface: str) -> str:
        """能否抓包：pcap/afpacket，或者 none（Windows 没装 Npcap）。"""
        try:
            io = self.backend.packet_io(iface, send_only=True)
        except PacketIOError as e:
            self.log("无法抓包：%s" % e, "debug")
            return "none"
        io.close()
        return io.kind

    def _start_watcher(self) -> None:
        if self._watcher and self._watcher.is_alive():
            return
        self._stop_watch.clear()
        self._watcher = threading.Thread(target=self._watch_carrier, name="carrier", daemon=True)
        self._watcher.start()

    def _watch_carrier(self) -> None:
        """换插另一台 B 时网线会断开再接上；接上后把直连配置补回来。"""
        last: Dict[str, str] = {}
        while not self._stop_watch.wait(1.0):
            for iface, link in list(self.links.items()):
                if iface in self._bouncing:
                    continue
                try:
                    carrier = self.backend.carrier(iface) is True
                except Exception:  # noqa: BLE001 - 查询失败当作没变化
                    continue
                prev = last.get(iface)
                last[iface] = carrier
                if prev is True and not carrier:
                    self.event({"event": "carrier", "iface": iface, "up": False})
                elif prev is False and carrier:
                    self.event({"event": "carrier", "iface": iface, "up": True})
                    try:
                        if link.ensure():
                            self.log("网线重新接上，已恢复直连网络配置 %s" % link.primary)
                    except Exception as e:  # noqa: BLE001
                        self.log("恢复直连网络配置失败：%s" % e, "error")

    def cmd_link_down(self, req: Dict) -> Dict:
        iface = self._iface(req)
        link = self.links.pop(iface, None)
        if link:
            link.down()
        return {"restored": bool(link)}

    def cmd_add_address(self, req: Dict) -> Dict:
        iface = self._iface(req)
        link = self.links.get(iface)
        if not link:
            raise HelperError("请先配置直连网卡")
        ipaddress.ip_interface(req["cidr"])
        return {"added": link.add_address(req["cidr"])}

    def cmd_addresses(self, req: Dict) -> Dict:
        iface = self._iface(req)
        return {"mac": self.backend.iface_mac(iface), "addresses": self.backend.iface_addresses(iface)}

    def cmd_sniff_start(self, req: Dict) -> Dict:
        iface = self._iface(req)
        self.cmd_sniff_stop({})
        own_ips = set()
        addrs = self.backend.iface_addresses(iface)
        for cidr in addrs["ipv4"]:
            own_ips.add(cidr.split("/")[0])
        own_ips.update(addrs["ipv6ll"])
        self.foreign_dhcp = False
        self.table = NeighborTable([self.backend.iface_mac(iface)], own_ips, self.event)
        sniffer = LinkSniffer(lambda: self.backend.packet_io(iface), self.table, iface)
        try:
            sniffer.start()
            self.sniffer = sniffer
            return {"started": True, "capture": sniffer.kind}
        except PacketIOError as e:
            # Windows 没装 Npcap：读系统邻居表代替抓包
            self.log("无法抓包（%s），改为读取系统邻居表" % e, "debug")
            poller = NeighborPoller(lambda: self.backend.neighbors(iface), self.table)
            poller.start()
            self.sniffer = poller
            return {"started": True, "capture": "none"}

    def cmd_sniff_stop(self, req: Dict) -> Dict:
        if self.sniffer:
            self.sniffer.stop()
            self.sniffer = None
        return {"stopped": True}

    def cmd_neighbors(self, req: Dict) -> Dict:
        return {"neighbors": self.table.snapshot() if self.table else [],
                "foreign_dhcp": self.foreign_dhcp}

    def cmd_arp_scan(self, req: Dict) -> Dict:
        iface = self._iface(req)
        specs = list(req.get("targets") or [])
        if req.get("common"):
            specs += COMMON_SUBNETS
        targets = expand_targets(specs)
        own = self.backend.iface_addresses(iface)["ipv4"]
        try:
            io = self.backend.packet_io(iface, send_only=True)
        except PacketIOError:
            return self._arp_scan_without_capture(iface, own, targets)
        try:
            sent = arp_scan(io, self.backend.iface_mac(iface), own, targets, int(req.get("pps", 2000)))
        finally:
            io.close()
        return {"sent": sent}

    def _arp_scan_without_capture(self, iface: str, own: List[str], targets: List[str]) -> Dict:
        """不能发原始报文时，用系统接口逐个解析本网段地址（在后台进行，结果作为 neighbor 事件上报）。"""
        work = [(t, sender_for(t, own)) for t in targets]
        work = [(t, s) for t, s in work if s]
        if not work or not self._arp_busy.acquire(blocking=False):
            return {"sent": 0, "skipped": len(targets)}

        def resolve(item):
            target, sender = item
            mac = self.backend.arp_resolve(iface, target, sender)
            if mac and self.table is not None:
                self.table.feed({"mac": mac, "ipv4": target})

        def scan():
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
                    list(pool.map(resolve, work))
            finally:
                self._arp_busy.release()

        threading.Thread(target=scan, name="sendarp", daemon=True).start()
        return {"sent": len(work), "skipped": len(targets) - len(work)}

    def cmd_probe6(self, req: Dict) -> Dict:
        """发出 IPv6 全节点 ping 就返回；回应由链路监听上报为 neighbor 事件。"""
        iface = self._iface(req)
        lls = self.backend.iface_addresses(iface).get("ipv6ll") or []
        return {"sent": probe_all_nodes(self.backend.ifindex(iface), src_ll=lls[0] if lls else None)}

    def cmd_bounce_link(self, req: Dict) -> Dict:
        iface = self._iface(req)
        link = self.links.get(iface)
        if not link:
            raise HelperError("请先配置直连网卡")
        self._bouncing.add(iface)
        try:
            how = link.bounce(req.get("mode", "quick"))
            if self.dhcp and self.dhcp.iface == iface and self.backend.dhcp_rebind_after_bounce:
                try:
                    self.dhcp.restart()
                except OSError as e:
                    self.log("网口重新启用后 DHCP 服务没能恢复：%s" % e, "error")
        finally:
            self._bouncing.discard(iface)
        return {"method": how}

    def cmd_dhcp_start(self, req: Dict) -> Dict:
        iface = self._iface(req)
        if self.foreign_dhcp and not req.get("force"):
            raise HelperError("该网口上已经有别的 DHCP 服务，为避免干扰别人的网络，没有启动 DHCP")
        self.cmd_dhcp_stop({})
        server = DhcpServer(
            iface,
            req["server_ip"],
            int(req["prefix"]),
            req["pool_start"],
            req["pool_end"],
            int(req.get("lease_time", 3600)),
            req.get("reservations") or {},
            self.event,
            socket_factory=lambda: self.backend.dhcp_socket(iface, req["server_ip"]),
            subnet_broadcast=self.backend.dhcp_subnet_broadcast,
        )
        server.start()
        self.dhcp = server
        self.log("DHCP 服务已在 %s 上启动（%s - %s，不下发网关/DNS）" % (
            iface, req["pool_start"], req["pool_end"]))
        return {"started": True}

    def cmd_dhcp_stop(self, req: Dict) -> Dict:
        if self.dhcp:
            self.dhcp.stop()
            self.dhcp = None
        return {"stopped": True}

    def cmd_leases(self, req: Dict) -> Dict:
        return {"leases": self.dhcp.snapshot() if self.dhcp else []}

    def cmd_cleanup(self, req: Dict) -> Dict:
        self.teardown()
        return {"cleaned": self.backend.cleanup_all(self.log)}

    def cmd_shutdown(self, req: Dict) -> Dict:
        self._shutdown.set()
        return {"bye": True}

    # ------------------------------------------------------------------ 主循环

    def teardown(self) -> None:
        self._stop_watch.set()
        self.cmd_dhcp_stop({})
        self.cmd_sniff_stop({})
        for iface in list(self.links):
            try:
                self.links.pop(iface).down()
            except Exception as e:  # noqa: BLE001
                self.log("恢复 %s 失败：%s" % (iface, e), "error")

    def dispatch(self, req: Dict) -> None:
        rid = req.get("id")
        cmd = req.get("cmd", "")
        fn: Optional[Callable[[Dict], Dict]] = getattr(self, "cmd_" + cmd, None)
        if not fn:
            self.send({"type": "response", "id": rid, "ok": False, "error": "未知命令：%s" % cmd})
            return
        try:
            result = fn(req)
            self.send({"type": "response", "id": rid, "ok": True, "result": result})
        except (HelperError, PacketIOError, OSError, ValueError, KeyError) + self._link_errors as e:
            self.send({"type": "response", "id": rid, "ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.send({"type": "response", "id": rid, "ok": False,
                       "error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()})

    def _lines(self, inp):
        try:
            for line in inp:
                yield line
        except (OSError, ValueError):  # 连接被重置：当作界面已退出
            return

    def serve(self, inp=None) -> None:
        inp = inp or sys.stdin
        try:
            for line in self._lines(inp):
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except ValueError:
                    self.log("收到无法解析的请求：%r" % line[:200], "error")
                    continue
                # 耗时命令放到线程里，保证事件和其它命令不被阻塞
                if req.get("cmd") in ("probe6", "arp_scan", "bounce_link", "link_up", "cleanup"):
                    threading.Thread(target=self.dispatch, args=(req,), daemon=True).start()
                else:
                    self.dispatch(req)
                if self._shutdown.is_set():
                    break
        finally:
            self.teardown()


def _say(msg: str) -> None:
    # Windows 上打包成窗口程序时没有标准输出
    if sys.stdout is not None:
        print(msg, flush=True)


def _log_to_file() -> None:
    """后台运行（macOS/Windows）时看不到输出，把错误写到状态目录下的 helper.log。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        sys.stderr = open(os.path.join(STATE_DIR, "helper.log"), "w", encoding="utf-8", buffering=1)
    except OSError:
        pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    address, token_file = parse_args(argv)
    if address:
        _log_to_file()
    backend = current_backend()
    if not backend.is_admin():
        _say(json.dumps({"type": "log", "level": "error", "msg": "网络助手需要管理员（root）权限"}))
        return 1
    if "--cleanup" in argv:
        done = backend.cleanup_all(_say)
        _say("已清理：%s" % (", ".join(done) or "无"))
        return 0

    sock = None
    if address:
        try:
            sock, inp, out = connect_back(address, token_file or "")
        except (OSError, TransportError) as e:
            print("连接界面失败：%s" % e, file=sys.stderr)
            return 2
        helper = Helper(out, backend)
    else:
        inp = sys.stdin
        helper = Helper(backend=backend)

    def on_signal(signum, frame):  # noqa: ARG001
        helper.teardown()
        os._exit(0)

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except (OSError, ValueError):
                pass
    helper.send({"type": "ready", "version": __version__, "protocol": HELPER_PROTOCOL})
    try:
        helper.serve(inp)
    finally:
        if sock is not None:
            sock.close()
    return 0
