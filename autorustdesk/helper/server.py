"""网络助手主循环：stdin 读取 JSON 请求，stdout 输出 JSON 响应和事件。

请求：{"id": 1, "cmd": "link_up", "iface": "enp3s0", "cidr": "192.168.77.1/24"}
响应：{"type": "response", "id": 1, "ok": true, "result": {...}}
事件：{"type": "event", "event": "lease", ...}
日志：{"type": "log", "level": "info", "msg": "..."}

stdin 关闭（界面退出或崩溃）时自动恢复网卡并退出。
"""

import ipaddress
import json
import os
import signal
import sys
import threading
import traceback
from typing import Any, Callable, Dict, Optional, Set

from .. import __version__
from . import HELPER_PROTOCOL
from .arp import COMMON_SUBNETS, arp_scan, expand_targets
from .dhcp import DhcpServer
from .icmp6 import probe_all_nodes
from .linkconfig import (
    LinkConfigurator,
    LinkError,
    cleanup_all,
    iface_addresses,
    iface_exists,
    iface_mac,
    read_sys,
)
from .sniffer import LinkSniffer, NeighborTable


class HelperError(Exception):
    pass


class Helper:
    def __init__(self, out=None) -> None:
        self.out = out or sys.stdout
        self._out_lock = threading.Lock()
        self.links: Dict[str, LinkConfigurator] = {}
        self.dhcp: Optional[DhcpServer] = None
        self.sniffer: Optional[LinkSniffer] = None
        self.table: Optional[NeighborTable] = None
        self.foreign_dhcp = False
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
            except (BrokenPipeError, ValueError):
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
        if not iface_exists(iface):
            raise HelperError("网卡不存在：%r" % iface)
        return iface

    def cmd_hello(self, req: Dict) -> Dict:
        return {"version": __version__, "protocol": HELPER_PROTOCOL, "uid": os.geteuid(),
                "pid": os.getpid(), "platform": sys.platform}

    def cmd_link_up(self, req: Dict) -> Dict:
        iface = self._iface(req)
        if iface in self.links:
            self.links[iface].down()
        link = LinkConfigurator(iface, self.log)
        result = link.up(req["cidr"])
        self.links[iface] = link
        self._start_watcher()
        return result

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
                carrier = read_sys(iface, "carrier")
                prev = last.get(iface)
                last[iface] = carrier
                if prev == "1" and carrier != "1":
                    self.event({"event": "carrier", "iface": iface, "up": False})
                elif prev is not None and prev != "1" and carrier == "1":
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
        return {"mac": iface_mac(iface), "addresses": iface_addresses(iface)}

    def cmd_sniff_start(self, req: Dict) -> Dict:
        iface = self._iface(req)
        self.cmd_sniff_stop({})
        own_ips = set()
        addrs = iface_addresses(iface)
        for cidr in addrs["ipv4"]:
            own_ips.add(cidr.split("/")[0])
        own_ips.update(addrs["ipv6ll"])
        self.foreign_dhcp = False
        self.table = NeighborTable([iface_mac(iface)], own_ips, self.event)
        self.sniffer = LinkSniffer(iface, self.table)
        self.sniffer.start()
        return {"started": True}

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
        sent = arp_scan(iface, iface_mac(iface), iface_addresses(iface)["ipv4"], targets,
                        int(req.get("pps", 2000)))
        return {"sent": sent}

    def cmd_probe6(self, req: Dict) -> Dict:
        """发出 IPv6 全节点 ping 就返回；回应由链路监听上报为 neighbor 事件。"""
        iface = self._iface(req)
        return {"sent": probe_all_nodes(iface)}

    def cmd_bounce_link(self, req: Dict) -> Dict:
        iface = self._iface(req)
        link = self.links.get(iface)
        if not link:
            raise HelperError("请先配置直连网卡")
        self._bouncing.add(iface)
        try:
            how = link.bounce(req.get("mode", "quick"))
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
        return {"cleaned": cleanup_all(self.log)}

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
        except (HelperError, LinkError, OSError, ValueError, KeyError) as e:
            self.send({"type": "response", "id": rid, "ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.send({"type": "response", "id": rid, "ok": False,
                       "error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()})

    def serve(self, inp=None) -> None:
        inp = inp or sys.stdin
        try:
            for line in inp:
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


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.geteuid() != 0:
        print(json.dumps({"type": "log", "level": "error", "msg": "网络助手需要 root 权限"}), flush=True)
        return 1
    helper = Helper()
    if argv[:1] == ["--cleanup"]:
        done = cleanup_all(lambda m: print(m))
        print("已清理：%s" % (", ".join(done) or "无"))
        return 0

    def on_signal(signum, frame):  # noqa: ARG001
        helper.teardown()
        os._exit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    helper.send({"type": "ready", "version": __version__, "protocol": HELPER_PROTOCOL})
    helper.serve()
    return 0
