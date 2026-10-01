"""完整流程：准备网络 → 发现 B → 登录 → 检查 → 安装 → 配置 → 验证 → 连接。

与界面无关：通过 Ui 回调输出进度、询问用户；图形界面和命令行共用。
"""

import dataclasses
import ipaddress
import json
import os
import posixpath
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

from ..bundle import RELEASES, Bundle, BundleError, release_label
from ..debian import version_compare
from ..helper.state import STATE_FILE as STALE_STATE_FILE
from . import nic as nicmod
from . import rustdesk_local
from .builtin import builtin_bundle
from .devices import DeviceRegistry, generate_password, now_str
from .helper_client import HelperClient, HelperError
from .settings import Settings, choose_subnet
from .ssh import AuthError, HostKeyMismatch, RemoteSession, SshError, SudoError, fingerprint

STEPS: List[Tuple[str, str]] = [
    ("network", "准备本机网络"),
    ("discover", "发现电脑 B"),
    ("login", "登录电脑 B"),
    ("probe", "检查 B 的环境"),
    ("install", "安装 RustDesk"),
    ("configure", "配置 RustDesk 与系统"),
    ("verify", "验证连通"),
    ("connect", "打开远程桌面"),
]
STEP_TITLES = dict(STEPS)

# 快速连接时，先用已经记住的地址试探 B，最多试这么多秒；连不上再走完整的发现流程
QUICK_PROBE_SECONDS = 3
# 连上记住的地址后，等链路监听确认这个地址确实是同一台设备，最多等这么多秒
QUICK_VERIFY_SECONDS = 1.5

PENDING, RUNNING, DONE, SKIPPED, FAILED, WARNING = (
    "pending", "running", "done", "skipped", "failed", "warning")

# Linux 上网络助手创建的 NetworkManager 连接配置名的前缀（与 helper/linkconfig.py 一致）
PROFILE_PREFIX = "AutoRustDesk-"
NPCAP_HINT = ("本机没有安装 Npcap，只能通过 DHCP 和 IPv6 发现电脑 B；B 用固定 IP 且关闭了 IPv6 时会找不到。"
              "建议安装 Npcap（https://npcap.com）")


class WorkflowError(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclasses.dataclass
class Credentials:
    username: str
    password: str = ""
    key_file: str = ""
    remember: bool = False


class Ui:
    """界面回调的默认实现（什么都不显示、全部同意）。"""

    def log(self, msg: str, level: str = "info") -> None:
        pass

    def step(self, step_id: str, state: str, detail: str = "") -> None:
        pass

    def device(self, info: Dict) -> None:
        pass

    def progress(self, text: str, fraction: float) -> None:
        pass

    def ask_credentials(self, title: str, username: str, error: str = "") -> Optional[Credentials]:
        return None

    def confirm(self, title: str, text: str, default: bool = True) -> bool:
        return default

    def choose(self, title: str, options: List[str]) -> Optional[int]:
        return 0 if options else None

    def is_cancelled(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Candidate:
    mac: str
    lease_ip: str = ""
    ipv4: Set[str] = dataclasses.field(default_factory=set)
    ipv6ll: Set[str] = dataclasses.field(default_factory=set)
    hostnames: Set[str] = dataclasses.field(default_factory=set)
    dhcp_client: bool = False
    dhcp_server: bool = False
    first_seen: float = dataclasses.field(default_factory=time.time)

    @property
    def hostname(self) -> str:
        return sorted(self.hostnames)[0] if self.hostnames else ""

    def describe(self) -> str:
        parts = [self.mac]
        if self.hostname:
            parts.append(self.hostname)
        if self.lease_ip:
            parts.append(self.lease_ip)
        parts.extend(sorted(self.ipv4 - {self.lease_ip}))
        return " · ".join(parts)


class Discovery:
    """汇总网络助手上报的事件（在助手的读线程里调用，需加锁）。"""

    def __init__(self, ui: Ui):
        self.ui = ui
        self.lock = threading.Lock()
        self.candidates: Dict[str, Candidate] = {}
        self.foreign_dhcp = False
        # 有新事件时置位，发现循环据此立即处理
        self.changed = threading.Event()

    def reset(self) -> None:
        with self.lock:
            self.candidates.clear()
            self.foreign_dhcp = False

    def _cand(self, mac: str) -> Candidate:
        mac = mac.lower()
        if mac not in self.candidates:
            self.candidates[mac] = Candidate(mac)
        return self.candidates[mac]

    def on_event(self, ev: Dict) -> None:
        kind = ev.get("event")
        self._handle(kind, ev)
        self.changed.set()

    def _handle(self, kind, ev: Dict) -> None:
        with self.lock:
            if kind == "lease":
                c = self._cand(ev["mac"])
                c.lease_ip = ev["ip"]
                if ev.get("hostname"):
                    c.hostnames.add(ev["hostname"])
                msg = "设备 %s 通过 DHCP 获得地址 %s" % (ev["mac"], ev["ip"])
                if ev.get("hostname"):
                    msg += "（主机名 %s）" % ev["hostname"]
                self.ui.log(msg)
            elif kind == "dhcp_discover":
                c = self._cand(ev["mac"])
                if not c.dhcp_client:
                    self.ui.log("设备 %s 正在请求 IP 地址（DHCP）" % ev["mac"])
                c.dhcp_client = True
                if ev.get("hostname"):
                    c.hostnames.add(ev["hostname"])
            elif kind == "neighbor":
                c = self._cand(ev["mac"])
                new_v4 = set(ev.get("ipv4", [])) - c.ipv4
                new_ll = set(ev.get("ipv6ll", [])) - c.ipv6ll
                c.ipv4 |= set(ev.get("ipv4", []))
                c.ipv6ll |= set(ev.get("ipv6ll", []))
                c.hostnames |= set(ev.get("hostnames", []))
                c.dhcp_client = c.dhcp_client or bool(ev.get("dhcp_client"))
                c.dhcp_server = c.dhcp_server or bool(ev.get("dhcp_server"))
                if new_v4 or new_ll:
                    self.ui.log("发现设备 %s：%s" % (
                        ev["mac"], "，".join(sorted(new_v4) + sorted(new_ll))), "debug")
            elif kind == "foreign_dhcp":
                self.foreign_dhcp = True
                self._cand(ev["mac"]).dhcp_server = True
                self.ui.log("注意：网口上有别的 DHCP 服务器（%s）" % ev["mac"], "warning")
            elif kind == "carrier":
                if ev.get("up"):
                    self.ui.log("网线已接上")
                else:
                    # 换插另一台设备：之前发现的设备都作废
                    self.candidates.clear()
                    self.foreign_dhcp = False
                    self.ui.log("网线已断开", "warning")

    def snapshot(self) -> List[Candidate]:
        with self.lock:
            return [dataclasses.replace(c, ipv4=set(c.ipv4), ipv6ll=set(c.ipv6ll),
                                        hostnames=set(c.hostnames))
                    for c in self.candidates.values()]


def stale_network_config() -> List[str]:
    """上次异常退出后遗留在本机的网络配置（普通用户也能读到）。"""
    found: List[str] = []
    try:
        with open(STALE_STATE_FILE, encoding="utf-8") as f:
            found.extend(json.load(f).keys())
    except (OSError, ValueError, AttributeError):
        pass
    if sys.platform.startswith("linux") and shutil.which("nmcli"):
        try:
            out = subprocess.run(["nmcli", "-t", "-f", "NAME", "connection", "show"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout
            found.extend(n for n in out.decode().splitlines() if n.startswith(PROFILE_PREFIX))
        except (OSError, subprocess.SubprocessError):
            pass
    return found


def tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def alias_for(ip: str) -> str:
    """为了访问固定 IP 的 B，A 在 B 的 /24 网段里临时使用的地址。"""
    net = ipaddress.ip_network("%s/24" % ip, strict=False)
    for last in (254, 253, 252):
        a = str(net.network_address + last)
        if a != ip:
            return "%s/24" % a
    return "%s/24" % (net.network_address + 250)


# ---------------------------------------------------------------------------
# 流程
# ---------------------------------------------------------------------------


class Workflow:
    def __init__(self, settings: Settings, registry: DeviceRegistry, ui: Ui):
        self.settings = settings
        self.registry = registry
        self.ui = ui
        self.discovery = Discovery(ui)
        self.helper = HelperClient(self._on_helper_event, self._on_helper_log)
        self._lock = threading.Lock()
        # 网络状态（多次运行之间保留，直到恢复网络）
        self.iface = ""
        self.scope = ""  # 访问 B 的 fe80:: 地址时 % 后面的部分
        self.net: Optional[ipaddress.IPv4Network] = None
        self.a_ip = ""
        self.dhcp_enabled = False
        self.ipv6_ok = True
        # 当前设备
        self.mac = ""
        self.ssh_host = ""
        self.link_ip = ""
        self.session: Optional[RemoteSession] = None
        self.facts: Dict = {}
        self.password = ""
        self.session_ssh_password = ""

    # ------------------------------------------------------------------ 助手事件

    def _on_helper_event(self, ev: Dict) -> None:
        self.discovery.on_event(ev)

    def _on_helper_log(self, msg: str, level: str) -> None:
        self.ui.log(msg, level)

    # ------------------------------------------------------------------ 工具

    def _check_cancel(self) -> None:
        if self.ui.is_cancelled():
            raise Cancelled()

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self._check_cancel()
            time.sleep(min(0.2, max(0.0, end - time.time())))

    def _device_info(self, **extra) -> Dict:
        dev = self.registry.get(self.mac) if self.mac else None
        info = {
            "mac": self.mac,
            "name": dev.title if dev else "",
            "ssh_host": self.ssh_host,
            "link_ip": self.link_ip,
            "password": self.password or (dev.rustdesk_password if dev else ""),
            "port": self.settings.rustdesk_port,
        }
        if dev:
            info.update(hostname=dev.hostname, rustdesk_version=dev.rustdesk_version,
                        rustdesk_id=dev.rustdesk_id, configured_at=dev.configured_at)
        info.update(extra)
        return info

    def bundle(self) -> Optional[Bundle]:
        """用户选的离线包；没选（或选的文件已经不在了）时用程序自带的。"""
        path = self.settings.bundle_path
        builtin = builtin_bundle()
        if path and not os.path.exists(path) and builtin:
            self.ui.log("之前选的离线包 %s 已经不在了，改用程序自带的离线包" % path, "warning")
            path = ""
        path = path or builtin
        if not path:
            return None
        try:
            return Bundle.open(path)
        except BundleError as e:
            raise WorkflowError(str(e))

    # ------------------------------------------------------------------ 入口

    def run(self, mode: str = "full") -> Dict:
        """mode: full（完整流程）/ quick（已配置过的设备直接连接）。"""
        errors = self.settings.validate()
        if errors:
            raise WorkflowError("设置有误：" + "；".join(errors))
        steps = [s for s, _ in STEPS]
        for s in steps:
            self.ui.step(s, PENDING)
        # 每次运行都重新确定设备，避免沿用上一台设备的信息
        self.mac = self.ssh_host = self.link_ip = self.password = ""
        self.facts = {}
        current = ""
        try:
            current = "network"
            self._run_step("network", self.step_network)
            current = "discover"
            direct = mode == "quick" and self._quick_direct()
            if direct:
                self.ui.step("discover", SKIPPED, "已记住的地址 %s 可以直接连接" % self.link_ip)
            else:
                self._run_step("discover", self.step_discover)
            quick_ok = bool(direct) or (mode == "quick" and self._quick_ready())
            if quick_ok:
                for s in ("login", "probe", "install", "configure"):
                    self.ui.step(s, SKIPPED, "已配置过，直接连接")
            else:
                for s in ("login", "probe", "install", "configure"):
                    current = s
                    self._run_step(s, getattr(self, "step_" + s))
            current = "verify"
            self._run_step("verify", self.step_verify)
            current = "connect"
            self._run_step("connect", self.step_connect)
            return {"ok": True, "mac": self.mac, "ip": self.link_ip, "password": self.password}
        except Cancelled:
            self.ui.step(current, FAILED, "已取消")
            self.ui.log("操作已取消", "warning")
            return {"ok": False, "cancelled": True}
        except (WorkflowError, HelperError, SshError, BundleError) as e:
            self.ui.step(current, FAILED, str(e))
            self.ui.log("%s失败：%s" % (STEP_TITLES.get(current, current), e), "error")
            return {"ok": False, "error": str(e), "step": current}
        except Exception as e:  # noqa: BLE001 - 意外错误也要在界面上标出失败的步骤
            msg = "%s: %s" % (type(e).__name__, e)
            self.ui.step(current, FAILED, msg)
            self.ui.log("%s出现意外错误：%s" % (STEP_TITLES.get(current, current), msg), "error")
            return {"ok": False, "error": msg, "step": current}
        finally:
            self._close_session()

    def _run_step(self, step_id: str, fn) -> None:
        self._check_cancel()
        self.ui.step(step_id, RUNNING)
        self.ui.log("—— %s ——" % STEP_TITLES[step_id])
        result = fn()
        if isinstance(result, tuple):
            state, detail = result
        else:
            state, detail = DONE, result or ""
        self.ui.step(step_id, state, detail)

    def _close_session(self) -> None:
        if self.session:
            try:
                self.session.cleanup()
            finally:
                self.session.close()
            self.session = None

    def restore_network(self) -> None:
        """停止 DHCP、恢复网卡原设置、退出网络助手。

        助手没有运行但有上次异常退出遗留的配置时，也会启动助手来清理。
        """
        self._close_session()
        if not self.helper.running and stale_network_config():
            self.ui.log("清理上次遗留的网络配置：%s" % "、".join(stale_network_config()), "warning")
            self.helper.start()
        if self.helper.running:
            try:
                self.helper.call("cleanup", timeout=60)
            except HelperError:
                pass
            self.helper.stop()
        self.iface = self.scope = ""
        self.net = None
        self.discovery.reset()

    # ------------------------------------------------------------------ 1 网络

    def step_network(self):
        s = self.settings
        nics = nicmod.list_nics(include_virtual=s.show_virtual_nics or bool(s.iface))
        chosen = None
        if s.iface:
            chosen = next((n for n in nics if n.name == s.iface), None)
            if chosen is None:
                raise WorkflowError("找不到网卡 %s" % s.iface)
        else:
            chosen = nicmod.pick_default(nics)
        if chosen is None:
            raise WorkflowError("没有找到有线网卡")
        iface = chosen.name
        if nicmod.carrier(iface) is not True:
            self.ui.log("网卡 %s 没有检测到网线，请插好网线并确认电脑 B 已开机……" % iface, "warning")
            deadline = time.time() + 90
            while time.time() < deadline and nicmod.carrier(iface) is not True:
                self._sleep(1)
            if nicmod.carrier(iface) is not True:
                raise WorkflowError("网卡 %s 没有检测到网线连接（B 没开机，或者网线/网口有问题）" % iface)

        if self.helper.running and self.iface == iface and self.net is not None:
            # 上次发现的设备可能已经换掉了，重新发现
            self.discovery.reset()
            return "沿用已配置的 %s（%s）" % (iface, self.a_ip)

        self.helper.start()
        used = []
        for name, cidrs in nicmod.ipv4_by_iface().items():
            if name != iface:
                used.extend(cidrs)
        net = choose_subnet(s.subnet, used)
        if net is None:
            raise WorkflowError("找不到与本机网络不冲突的直连网段，请在设置里指定一个")
        if net != s.network:
            self.ui.log("直连网段 %s 与本机其它网络冲突，改用 %s" % (s.subnet, net), "warning")
        a_cidr = s.a_address(net)
        self.discovery.reset()
        res = self.helper.call("link_up", iface=iface, cidr=str(a_cidr), timeout=90)
        self.iface, self.net, self.a_ip = iface, net, str(a_cidr.ip)
        self.scope = str(res.get("scope") or iface)
        if res.get("capture") == "none":
            self.ui.log(NPCAP_HINT, "warning")
        self.helper.call("sniff_start", iface=iface)
        if res.get("foreign_dhcp"):
            # 配置之前网卡就从别的 DHCP 服务器拿到了地址（网关也通），插的多半是局域网
            self.discovery.foreign_dhcp = True
        self.ui.log("观察链路 2 秒，确认是直连而不是局域网……")
        self._sleep(2)
        others = [c for c in self.discovery.snapshot()]
        if self.discovery.foreign_dhcp or len(others) > 2:
            text = ("这个网口上发现了%s，看起来连着的是一个局域网而不是直连的电脑 B。\n"
                    "为避免干扰别人的网络，不会启动本程序的 DHCP 服务。是否继续查找 B？") % (
                "其它 DHCP 服务器" if self.discovery.foreign_dhcp else "%d 台设备" % len(others))
            if not self.ui.confirm("网口连着局域网？", text, default=False):
                raise Cancelled()
            self.dhcp_enabled = False
        else:
            pool = s.pool(net)
            self.helper.call("dhcp_start", iface=iface, server_ip=self.a_ip, prefix=net.prefixlen,
                             pool_start=pool[0], pool_end=pool[1], lease_time=s.lease_time,
                             reservations=self.registry.reservations(net))
            self.dhcp_enabled = True
        backend = {"networkmanager": "NetworkManager", "iproute2": "ip 命令", "ifconfig": "ifconfig",
                   "netsh": "netsh"}.get(res.get("backend"), "")
        return "%s = %s（%s）" % (iface, a_cidr, backend)

    # ------------------------------------------------------------------ 2 发现

    def step_discover(self):
        s = self.settings
        start = time.time()
        next_probe = next_scan = 0.0
        ipv6_sent = False
        ipv6_errors = 0
        deep_scanned = False
        bounces: List[str] = []
        warned_ssh = False
        hints = {
            40: "还没发现电脑 B。请确认 B 已开机、网线两头都插好（网口指示灯应该亮）。",
            90: "如果仍然找不到，可以手动把网线拔下再插上；B 的网口也可能被禁用，需要在 B 上检查网络设置。",
        }
        while True:
            self._check_cancel()
            now = time.time()
            elapsed = now - start
            for t in sorted(hints):
                if elapsed >= t:
                    self.ui.log(hints.pop(t), "warning")
            # IPv6 全节点 ping：发出即返回，回应由链路监听收到。
            # 网卡的 IPv6 地址刚配置时还不能用（sent 为 0），过一会儿再试；连续出错 3 次才放弃
            if self.ipv6_ok and now >= next_probe:
                next_probe = now + (3 if ipv6_sent else 1)
                try:
                    if self.helper.call("probe6", iface=self.iface, timeout=10).get("sent"):
                        ipv6_sent = True
                    ipv6_errors = 0
                except HelperError as e:
                    ipv6_errors += 1
                    if ipv6_errors >= 3:
                        self.ipv6_ok = False
                        self.ui.log("IPv6 探测不可用（%s），改用其它方式发现设备" % e, "debug")
            # 直连网段 ARP 扫描（254 个地址约 0.1 秒）：B 已有地址但不发报文时靠它发现
            if now >= next_scan:
                next_scan = now + (3 if elapsed < 30 else 10)
                try:
                    self.helper.call("arp_scan", iface=self.iface, targets=[str(self.net)], timeout=30)
                except HelperError as e:
                    self.ui.log("ARP 扫描失败：%s" % e, "debug")
            # 常见网段 ARP 探测：B 是固定 IP、不发报文、又没开 IPv6 时的办法
            if not deep_scanned and elapsed > 3 and not self.discovery.snapshot():
                deep_scanned = True
                self.ui.log("在常见网段中查找固定 IP 的电脑 B……")
                try:
                    self.helper.call("arp_scan", iface=self.iface, common=True, pps=4000, timeout=60)
                except HelperError as e:
                    self.ui.log("ARP 扫描失败：%s" % e, "debug")
            cands = [c for c in self.discovery.snapshot() if not c.dhcp_server]
            cand = self._pick_candidate(cands)
            if cand is not None:
                host = self._reachable_host(cand)
                if host:
                    self.mac = cand.mac
                    self.ssh_host = host
                    self.link_ip = self._in_subnet_ip(cand)
                    dev = self.registry.update(cand.mac, last_seen=now_str(),
                                               hostname=cand.hostname or (self.registry.get_or_create(cand.mac).hostname))
                    self.ui.device(self._device_info(hostname=cand.hostname))
                    known = "（已知设备：%s）" % dev.title if dev.configured_at else "（新设备）"
                    return "%s %s（用时 %.0f 秒）" % (cand.describe(), known, time.time() - start)
                if not warned_ssh and time.time() - cand.first_seen > 30:
                    warned_ssh = True
                    self.ui.log("已发现设备 %s，但连不上它的 SSH 端口 %d，请确认 B 上开启了 SSH 服务" % (
                        cand.describe(), s.ssh_port), "warning")
            # 电子"拔插网线"：B 上的 NetworkManager 在 DHCP 连续失败后会停 5 分钟才重试，
            # 但网线重新接上时会立刻重试。B 还没有任何地址、也没在请求地址时，就让它看到一次断开再接上。
            waiting_for_b = not any(self._has_address(c) or c.dhcp_client for c in cands)
            if self.dhcp_enabled and waiting_for_b:
                if not bounces and elapsed > 5:
                    bounces.append("quick")
                    self._bounce("quick")
                elif len(bounces) == 1 and elapsed > 25:
                    bounces.append("long")
                    self._bounce("long")
            if time.time() - start > s.discover_timeout:
                raise WorkflowError("%d 秒内没有发现电脑 B（或连不上它的 SSH）" % s.discover_timeout)
            # 有新的发现立即处理，否则最多等 0.3 秒
            self.discovery.changed.wait(0.3)
            self.discovery.changed.clear()

    def _has_address(self, c: Candidate) -> bool:
        return bool(c.lease_ip or c.ipv4 or c.ipv6ll)

    def _bounce(self, mode: str) -> None:
        self.ui.log("B 还没有来要 IP 地址（它的 DHCP 可能在等待重试），"
                    "让网口断开再接上一次，相当于自动拔插网线……")
        try:
            r = self.helper.call("bounce_link", iface=self.iface, mode=mode, timeout=60)
            self.ui.log("已%s，等待 B 重新获取地址" % r.get("method", "重新接通网口"), "debug")
        except HelperError as e:
            self.ui.log("自动拔插网口失败：%s（可以手动把网线拔下再插上）" % e, "warning")

    def _pick_candidate(self, cands: List[Candidate]) -> Optional[Candidate]:
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        with_lease = [c for c in cands if self._in_subnet_ip(c)]
        if len(with_lease) == 1:
            return with_lease[0]
        options = [c.describe() for c in cands]
        idx = self.ui.choose("链路上有多台设备，请选择电脑 B", options)
        if idx is None:
            raise Cancelled()
        return cands[idx]

    def _in_subnet_ip(self, c: Candidate) -> str:
        """B 在直连网段里的地址（DHCP 分配的，或者上次留下的）。"""
        if c.lease_ip:
            return c.lease_ip
        for ip in sorted(c.ipv4):
            if self.net is not None and ipaddress.ip_address(ip) in self.net:
                return ip
        return ""

    def _reachable_host(self, c: Candidate) -> str:
        port = self.settings.ssh_port
        # 直连链路往返不到 1 毫秒，1 秒超时足够
        direct = self._in_subnet_ip(c)
        if direct and tcp_open(direct, port, 1):
            return direct
        for ll in sorted(c.ipv6ll):
            host = "%s%%%s" % (ll, self.scope or self.iface)
            if tcp_open(host, port, 1):
                return host
        static = sorted(ip for ip in c.ipv4 if self.net is None or ipaddress.ip_address(ip) not in self.net)
        if static and (not self.ipv6_ok or not c.ipv6ll or time.time() - c.first_seen > 6):
            for ip in static:
                alias = alias_for(ip)
                try:
                    if self.helper.call("add_address", iface=self.iface, cidr=alias).get("added"):
                        self.ui.log("B 使用固定 IP %s，本机临时添加地址 %s 以便访问" % (ip, alias))
                except HelperError as e:
                    self.ui.log("添加临时地址失败：%s" % e, "warning")
                    continue
                if tcp_open(ip, port, 1):
                    return ip
        return ""

    def _quick_direct(self) -> bool:
        """快速连接：不等 B 来要地址，直接试探上次记住的地址。

        B 的地址是本程序的 DHCP 按 MAC 固定分配的，B 通常还保留着上次的地址。试探通了就
        不用再发现 B（也不用等 DHCP、拔插网口）。不通、链路上不止一台设备、或者这个地址现在
        是另一台设备时返回 False，回到完整的发现流程。
        """
        if self.net is None:
            return False
        port = self.settings.rustdesk_port
        known = []
        for dev in self.registry.all():
            if not (dev.rustdesk_password and dev.configured_at and dev.ip):
                continue
            try:
                if ipaddress.ip_address(dev.ip) in self.net:
                    known.append(dev)
            except ValueError:
                continue
        if not known:
            return False
        self.ui.log("先试探已记住的地址：%s" % "、".join("%s（%s）" % (d.ip, d.title) for d in known))
        deadline = time.time() + QUICK_PROBE_SECONDS
        while True:
            self._check_cancel()
            # 直连链路往返不到 1 毫秒，1 秒超时足够
            reachable = [d for d in known if tcp_open(d.ip, port, 1)]
            if len(reachable) > 1:
                self.ui.log("链路上有多个记住的地址都能连上，改用完整的发现流程确认是哪一台", "warning")
                return False
            if reachable:
                dev = reachable[0]
                if not self._same_device(dev):
                    return False
                self.mac, self.ssh_host, self.link_ip = dev.mac, dev.ip, dev.ip
                self.password = dev.rustdesk_password
                self.registry.update(dev.mac, last_seen=now_str())
                self.ui.device(self._device_info())
                self.ui.log("已记住的地址 %s（%s）可以直接连接，跳过发现" % (dev.ip, dev.title))
                return True
            if time.time() >= deadline:
                self.ui.log("已记住的地址暂时连不上，按完整流程重新发现 B", "debug")
                return False
            self._sleep(0.5)

    def _same_device(self, dev) -> bool:
        """链路监听看到过这个地址的 MAC 时核对一下；没看到就当作是同一台。"""
        end = time.time() + QUICK_VERIFY_SECONDS
        while True:
            for c in self.discovery.snapshot():
                if dev.ip == c.lease_ip or dev.ip in c.ipv4:
                    if c.mac.lower() == dev.mac.lower():
                        return True
                    self.ui.log("地址 %s 现在属于另一台设备（%s），不是 %s（%s），改用完整的发现流程" % (
                        dev.ip, c.mac, dev.title, dev.mac), "warning")
                    return False
            if time.time() >= end:
                return True
            self._sleep(0.2)

    def _quick_ready(self) -> bool:
        dev = self.registry.get(self.mac)
        if not dev or not dev.rustdesk_password or not dev.configured_at:
            return False
        ip = self.link_ip or dev.ip
        if ip and tcp_open(ip, self.settings.rustdesk_port, 3):
            self.link_ip = ip
            self.password = dev.rustdesk_password
            return True
        return False

    # ------------------------------------------------------------------ 3 登录

    def step_login(self):
        s = self.settings
        dev = self.registry.get_or_create(self.mac)
        user = dev.ssh_user or s.ssh_user
        password = dev.ssh_password or self.session_ssh_password or (
            s.ssh_password if s.remember_ssh_password else "")
        key_file = s.ssh_key_file
        remember = s.remember_ssh_password
        error = ""
        for _attempt in range(4):
            self._check_cancel()
            if not user or not (password or key_file) or error:
                creds = self.ui.ask_credentials("登录电脑 B（%s）" % self.ssh_host, user, error)
                if creds is None:
                    raise Cancelled()
                user, password, remember = creds.username, creds.password, creds.remember
                key_file = creds.key_file or key_file
            sess = RemoteSession(self.ssh_host, user, password, s.ssh_port, key_file)
            try:
                try:
                    host_key = sess.connect(expected_host_key=dev.host_key)
                except HostKeyMismatch as e:
                    text = ("这台设备（MAC %s）的 SSH 主机密钥和上次记录的不一样：\n上次：%s\n现在：%s\n"
                            "如果 B 重装过系统，这是正常的；否则可能连错了设备。是否继续？") % (
                        self.mac, fingerprint(e.expected), fingerprint(e.actual))
                    if not self.ui.confirm("主机密钥变化", text, default=False):
                        raise Cancelled()
                    host_key = sess.connect(expected_host_key="")
                sess.check_sudo()
            except (AuthError, SudoError) as e:
                sess.close()
                error = str(e)
                password = ""
                continue
            self.session = sess
            self.session_ssh_password = password
            self.registry.update(self.mac, ssh_user=user, host_key=host_key,
                                 ssh_password=password if remember else "")
            s.ssh_user = user
            if remember:
                s.remember_ssh_password = True
            try:
                s.save()
            except OSError:
                pass
            return "%s@%s（主机密钥 %s）" % (user, self.ssh_host, fingerprint(host_key)[:23] + "…")
        raise WorkflowError(error or "登录失败")

    # ------------------------------------------------------------------ 4 检查

    def _ard(self, sub: str, args: List[str], timeout: float = 600) -> Dict:
        assert self.session is not None
        return self.session.run_ard(
            sub, args,
            on_log=lambda m: self.ui.log("B: " + m),
            on_step=lambda m: self.ui.log("B: " + m),
            on_output=lambda m: self.ui.log(m, "debug"),
            timeout=timeout,
        )

    def step_probe(self):
        assert self.session is not None
        self.session.make_workdir()
        self.session.upload_remote_script()
        res = self._ard("probe", ["--link-mac", self.mac, "--port", str(self.settings.rustdesk_port)],
                        timeout=180)
        f = self.facts = res["facts"]
        if f.get("arch") != "amd64":
            raise WorkflowError("B 的架构是 %s，离线包只支持 x86_64" % f.get("arch"))
        rd = f["rustdesk"]
        disp = f["display"]
        state = DONE
        codename = f["os"].get("codename", "")
        pretty = f["os"].get("pretty") or codename or "?"
        if codename not in RELEASES:
            self.ui.log("注意：B 是 %s，不在测试过的版本（%s）之内" % (
                pretty, "、".join(release_label(c) for c in RELEASES)), "warning")
            state = WARNING
        if not disp.get("xorg_usable", True):
            text = ("B 是 %s，只提供 Wayland 桌面（Ubuntu 26.04 起 GNOME 不再提供 Xorg 会话）。\n\n"
                    "本程序可以安装并配置 RustDesk，但是：\n"
                    "· RustDesk 无法控制登录界面，B 没接显示器时也无法使用；\n"
                    "· 需要有人在 B 上登录桌面后才能远程，连接时 B 上可能要确认共享屏幕。\n\n"
                    "是否继续？") % pretty
            if not self.ui.confirm("B 只能部分支持", text, default=False):
                raise Cancelled()
            state = WARNING
        if disp.get("xorg_conf"):
            self.ui.log("注意：B 上有 /etc/X11/xorg.conf，可能导致虚拟显示器不生效", "warning")
        if disp.get("nvidia"):
            self.ui.log("注意：B 使用 NVIDIA 驱动，虚拟显示器可能需要额外配置", "warning")
        parts = [
            f["os"].get("pretty") or "?",
            "RustDesk %s%s" % (rd.get("version") or "未安装",
                               "（服务运行中）" if rd.get("service_active") == "active" else ""),
            "登录界面 %s" % ("Xorg" if disp.get("gdm_wayland") == "disabled" else
                            "Wayland" if disp.get("xorg_usable", True) else "Wayland（没有 Xorg）"),
            "显示器 %s" % ("、".join(disp.get("monitors") or []) or "未连接"),
            "已登录用户 %d 个" % disp.get("user_sessions", 0),
        ]
        dev = self.registry.update(self.mac, hostname=f.get("hostname", ""),
                                   rustdesk_version=rd.get("version") or "")
        self.ui.device(self._device_info(hostname=dev.hostname, facts=f))
        return state, " · ".join(parts)

    # ------------------------------------------------------------------ 5 安装

    def step_install(self):
        rd = self.facts["rustdesk"]
        disp = self.facts["display"]
        bundle = self.bundle()
        installed = rd.get("version")
        # 只有 Wayland 的系统用不上虚拟显示驱动（它是给 Xorg 用的）
        need_dummy = (self.settings.headless in ("auto", "on") and not disp.get("dummy_driver")
                      and disp.get("xorg_usable", True))
        need_rd = not installed or bool(
            bundle and version_compare(installed, bundle.rustdesk_version) < 0)
        if not need_rd and not need_dummy:
            return SKIPPED, "已安装 RustDesk %s" % installed
        if bundle is None:
            if not installed:
                raise WorkflowError("B 上没有安装 RustDesk，请先选择离线包")
            self.ui.log("B 缺少虚拟显示驱动，但没有选择离线包，无法安装", "warning")
            return WARNING, "未选择离线包，跳过"
        if installed and need_rd:
            if not self.ui.confirm("升级 RustDesk？", "B 上的 RustDesk 是 %s，离线包是 %s，是否升级？" % (
                    installed, bundle.rustdesk_version), default=True):
                need_rd = False
                if not need_dummy:
                    return SKIPPED, "保留 RustDesk %s" % installed
        codename = self.facts["os"].get("codename", "")
        if not bundle.has_release(codename):
            raise WorkflowError("离线包里没有 %s 的依赖（离线包包含：%s）。请用「制作离线包」重新制作，并勾选 %s" % (
                release_label(codename), "、".join(release_label(c) for c in bundle.releases),
                release_label(codename)))
        packages = [p for p in bundle.install_packages
                    if (p == "rustdesk" and need_rd) or (p != "rustdesk" and need_dummy)]
        assert self.session is not None
        # 只上传 B 这个版本需要的那部分
        fd, local = tempfile.mkstemp(suffix=".tar", prefix="ard-bundle-")
        os.close(fd)
        try:
            bundle.write_release_tar(codename, local)
            remote = posixpath.join(self.session.workdir, "bundle.tar")
            size = os.path.getsize(local)
            self.ui.log("上传离线包（%.1f MB）……" % (size / 1048576.0))
            last = [0.0]

            def progress(done: int, total: int) -> None:
                frac = done / float(total or 1)
                if frac - last[0] >= 0.02 or done == total:
                    last[0] = frac
                    self.ui.progress("上传离线包", frac)
                self._check_cancel()

            self.session.put(local, remote, progress)
            self.ui.progress("", -1)
        finally:
            os.remove(local)
        res = self._ard("install", ["--bundle", remote, "--packages"] + packages, timeout=3600)
        ver = res.get("rustdesk_version") or installed or ""
        self.registry.update(self.mac, rustdesk_version=ver)
        self.facts["rustdesk"]["version"] = ver
        return "RustDesk %s 已安装" % ver

    # ------------------------------------------------------------------ 6 配置

    def step_configure(self):
        s = self.settings
        assert self.session is not None and self.net is not None
        dev = self.registry.get_or_create(self.mac)
        preexisting = bool(self.facts.get("rustdesk", {}).get("installed")) and not dev.configured_at
        if s.password_mode == "fixed":
            password = s.fixed_password
        else:
            password = dev.rustdesk_password or generate_password()
        if preexisting and not dev.rustdesk_password:
            text = ("B 上原来就装有 RustDesk。\n接下来会开启 IP 直连，并把 RustDesk 的固定密码设为新密码"
                    "（原来的固定密码将失效）。是否继续？")
            if not self.ui.confirm("修改 B 上的 RustDesk 设置", text, default=True):
                raise Cancelled()
        # 先保存密码再配置，避免配置中途失败丢失密码
        self.registry.update(self.mac, rustdesk_password=password)
        self.password = password
        pwfile = posixpath.join(self.session.workdir, "rustdesk-password")
        self.session.write_file(pwfile, password.encode(), 0o600)
        args = [
            "--password-file", pwfile,
            "--port", str(s.rustdesk_port),
            "--headless", s.headless,
            "--resolution", s.resolution,
            "--restart-dm", "auto",
            "--firewall-subnet", str(self.net),
        ]
        if s.whitelist:
            args += ["--whitelist", str(self.net)]
        if s.prevent_sleep:
            args.append("--prevent-sleep")
        res = self._ard("configure", args, timeout=900)
        for c in res.get("changes", []):
            self.ui.log("B: 已修改：" + c)
        for w in res.get("warnings", []):
            self.ui.log("B: " + w, "warning")
        if res.get("needs_restart"):
            if self.ui.confirm("需要重启 B 的登录界面",
                               "B 上有用户正在使用图形界面。显示设置要重启登录界面后才生效，"
                               "这会让该用户退出登录。现在重启吗？", default=False):
                self._ard("restart-display", ["--force"], timeout=180)
        self.registry.update(self.mac, rustdesk_version=res.get("rustdesk_version") or "",
                             rustdesk_id=res.get("id") or "", configured_at=now_str())
        self.ui.device(self._device_info())
        state = WARNING if res.get("warnings") else DONE
        return state, "已修改 %d 项%s" % (len(res.get("changes", [])),
                                        "，有 %d 条提醒" % len(res["warnings"]) if res.get("warnings") else "")

    # ------------------------------------------------------------------ 7 验证

    def step_verify(self):
        s = self.settings
        assert self.net is not None
        dev = self.registry.get_or_create(self.mac)
        if not self.link_ip or ipaddress.ip_address(self.link_ip) not in self.net:
            if self.session is None:
                raise WorkflowError("B 不是通过 DHCP 获得的地址，需要完整流程来配置链路地址")
            ip = self.registry.assign_ip(self.mac, self.net, s.pool(self.net))
            res = self._ard("link-ip", ["--mac", self.mac, "--cidr", "%s/%d" % (ip, self.net.prefixlen)],
                            timeout=60)
            self.link_ip = res["address"].split("/")[0]
        self.registry.update(self.mac, ip=self.link_ip)
        self.password = self.password or dev.rustdesk_password
        deadline = time.time() + 45
        while time.time() < deadline:
            if tcp_open(self.link_ip, s.rustdesk_port, 3):
                self.ui.device(self._device_info())
                return "%s:%d 可以连接" % (self.link_ip, s.rustdesk_port)
            self._sleep(2)
        raise WorkflowError("连不上 B 的 RustDesk 端口 %s:%d（B 上的 RustDesk 可能没有图形会话）" % (
            self.link_ip, s.rustdesk_port))

    # ------------------------------------------------------------------ 8 连接

    def step_connect(self):
        s = self.settings
        manual = "请在 RustDesk 中输入地址 %s，密码 %s" % (self.link_ip, self.password)
        if not s.auto_launch:
            return SKIPPED, manual
        prefix = rustdesk_local.find_client(s.rustdesk_client)
        if not prefix:
            self.ui.log("本机没有找到 RustDesk 客户端。" + manual, "warning")
            return WARNING, "未找到本机 RustDesk；" + manual
        rustdesk_local.launch(prefix, self.link_ip, self.password)
        self.ui.log("已打开 RustDesk 连接 %s。B 没有登录时，会看到 B 的登录界面，输入 B 的系统密码即可登录。" % self.link_ip)
        return "已连接 %s" % self.link_ip
