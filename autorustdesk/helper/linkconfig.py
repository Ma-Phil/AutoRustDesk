"""配置电脑 A 的直连网卡（Linux），并能恢复原状。

- NetworkManager 管理的网卡：新建一个临时连接配置 AutoRustDesk-<网卡>，
  手动地址、不设网关（never-default）、IPv6 只用链路本地；结束时删除，
  NetworkManager 会自动切回原来的连接。
- 其它情况：直接用 ip 命令加地址，结束时删掉。
- ufw 启用时临时放行该网卡上的 DHCP 请求（UDP 67）。

所做的修改记录在 /run/autorustdesk/links.json，助手异常退出后可以 cleanup。
"""

import ipaddress
import json
import os
import shutil
import subprocess
from typing import Callable, Dict, List, Optional, Tuple

STATE_DIR = "/run/autorustdesk"
STATE_FILE = os.path.join(STATE_DIR, "links.json")
PROFILE_PREFIX = "AutoRustDesk-"

LogFn = Callable[[str], None]


class LinkError(Exception):
    pass


def run(cmd: List[str], timeout: float = 30) -> Tuple[int, str]:
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        p = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, env=env,
        )
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "%s: 命令不存在" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "%s: 超时" % " ".join(cmd)


def iface_exists(iface: str) -> bool:
    return bool(iface) and "/" not in iface and os.path.isdir("/sys/class/net/%s" % iface)


def read_sys(iface: str, name: str) -> str:
    try:
        with open("/sys/class/net/%s/%s" % (iface, name)) as f:
            return f.read().strip()
    except OSError:
        return ""


def iface_mac(iface: str) -> str:
    return read_sys(iface, "address").lower()


def iface_addresses(iface: str) -> Dict[str, List[str]]:
    rc, out = run(["ip", "-j", "addr", "show", "dev", iface])
    result: Dict[str, List[str]] = {"ipv4": [], "ipv6ll": [], "ipv6": []}
    if rc != 0:
        return result
    try:
        data = json.loads(out)
    except ValueError:
        return result
    for item in data:
        for a in item.get("addr_info", []):
            cidr = "%s/%s" % (a.get("local"), a.get("prefixlen"))
            if a.get("family") == "inet":
                result["ipv4"].append(cidr)
            elif a.get("family") == "inet6":
                key = "ipv6ll" if a.get("scope") == "link" else "ipv6"
                result[key].append(a.get("local"))
    return result


def all_ipv4_networks(exclude_iface: Optional[str] = None) -> List[Tuple[str, str]]:
    """A 上其它网卡已用的 IPv4 网段，用于检查直连网段是否冲突。"""
    rc, out = run(["ip", "-j", "addr", "show"])
    result: List[Tuple[str, str]] = []
    if rc != 0:
        return result
    try:
        data = json.loads(out)
    except ValueError:
        return result
    for item in data:
        name = item.get("ifname")
        if name == exclude_iface or name == "lo":
            continue
        for a in item.get("addr_info", []):
            if a.get("family") == "inet":
                net = ipaddress.ip_interface("%s/%s" % (a["local"], a["prefixlen"])).network
                result.append((name, str(net)))
    return result


def nm_running() -> bool:
    if not shutil.which("nmcli"):
        return False
    rc, out = run(["nmcli", "-t", "-f", "RUNNING", "general"])
    return rc == 0 and out.strip() == "running"


def nm_device_state(iface: str) -> str:
    rc, out = run(["nmcli", "-t", "-f", "DEVICE,STATE", "device"])
    if rc != 0:
        return ""
    for line in out.splitlines():
        dev, _, state = line.partition(":")
        if dev == iface:
            return state
    return ""


def ufw_active() -> bool:
    if not shutil.which("ufw"):
        return False
    rc, out = run(["ufw", "status"])
    return rc == 0 and "Status: active" in out


def _load_state() -> Dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: Dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


class LinkConfigurator:
    def __init__(self, iface: str, log: LogFn):
        if not iface_exists(iface):
            raise LinkError("网卡不存在：%s" % iface)
        self.iface = iface
        self.log = log
        self.backend = ""
        self.profile = PROFILE_PREFIX + iface
        self.primary = ""  # 直连网段里本机的地址
        self.addresses: List[str] = []  # 用 ip 命令加上的地址
        self.ufw_rule = False
        self.ipv6_was_disabled: Optional[str] = None

    # ------------------------------------------------------------------ 状态记录

    def _record(self) -> None:
        state = _load_state()
        state[self.iface] = {
            "backend": self.backend,
            "profile": self.profile,
            "addresses": self.addresses,
            "ufw_rule": self.ufw_rule,
            "ipv6_was_disabled": self.ipv6_was_disabled,
        }
        _save_state(state)

    def _forget(self) -> None:
        state = _load_state()
        state.pop(self.iface, None)
        _save_state(state)

    # ------------------------------------------------------------------ 配置

    def up(self, cidr: str) -> Dict:
        want = ipaddress.ip_interface(cidr)
        if not (want.ip.is_private or want.ip.is_link_local):
            raise LinkError("直连网段必须是私有地址：%s" % cidr)
        for name, net in all_ipv4_networks(exclude_iface=self.iface):
            if ipaddress.ip_network(net).overlaps(want.network):
                raise LinkError("直连网段 %s 与本机网卡 %s 的网段 %s 冲突，请在设置里换一个" % (want.network, name, net))
        if read_sys(self.iface, "carrier") != "1":
            run(["ip", "link", "set", self.iface, "up"])
        self._enable_ipv6()
        self.primary = str(want)
        if nm_running() and nm_device_state(self.iface) not in ("", "unmanaged"):
            self._nm_up(str(want))
        else:
            self._ip_up(str(want))
        self._ufw_allow()
        self._record()
        addrs = iface_addresses(self.iface)
        return {"backend": self.backend, "cidr": str(want), "mac": iface_mac(self.iface),
                "addresses": addrs, "carrier": read_sys(self.iface, "carrier") == "1"}

    def _enable_ipv6(self) -> None:
        path = "/proc/sys/net/ipv6/conf/%s/disable_ipv6" % self.iface
        try:
            with open(path) as f:
                value = f.read().strip()
            if value == "1":
                with open(path, "w") as f:
                    f.write("0")
                self.ipv6_was_disabled = value
                self.log("已临时开启 %s 的 IPv6（用于发现设备）" % self.iface)
        except OSError:
            pass

    def _nm_up(self, cidr: str) -> None:
        self.backend = "networkmanager"
        self._nm_delete_stale()
        rc, out = run([
            "nmcli", "connection", "add", "type", "ethernet", "ifname", self.iface,
            "con-name", self.profile, "autoconnect", "no",
            "ipv4.method", "manual", "ipv4.addresses", cidr, "ipv4.never-default", "yes",
            "ipv4.ignore-auto-dns", "yes", "ipv6.method", "link-local",
        ])
        if rc != 0:
            raise LinkError("创建 NetworkManager 连接失败：%s" % out.strip())
        rc, out = run(["nmcli", "--wait", "20", "connection", "up", self.profile,
                       "ifname", self.iface], timeout=40)
        if rc != 0:
            run(["nmcli", "connection", "delete", self.profile])
            hint = "（网线没插好或对端没开机？）" if "carrier" in out.lower() else ""
            raise LinkError("启用直连配置失败%s：%s" % (hint, out.strip()))
        self.log("已通过 NetworkManager 配置 %s = %s（不设网关）" % (self.iface, cidr))

    def _nm_delete_stale(self) -> None:
        rc, out = run(["nmcli", "-t", "-f", "NAME", "connection", "show"])
        if rc != 0:
            return
        for name in out.splitlines():
            if name.strip() == self.profile:
                run(["nmcli", "connection", "delete", name.strip()])

    def _ip_up(self, cidr: str) -> None:
        self.backend = "iproute2"
        run(["ip", "link", "set", self.iface, "up"])
        self.add_address(cidr)
        self.log("已用 ip 命令配置 %s = %s（不设网关）" % (self.iface, cidr))

    def add_address(self, cidr: str) -> bool:
        """给网卡再加一个地址（例如适配 B 的固定 IP 网段），结束时删除。"""
        want = ipaddress.ip_interface(cidr)
        present = [ipaddress.ip_interface(a) for a in iface_addresses(self.iface)["ipv4"]]
        if want in present:
            return False
        rc, out = run(["ip", "addr", "add", str(want), "dev", self.iface])
        if rc != 0 and "File exists" not in out:
            raise LinkError("添加地址失败：%s" % out.strip())
        self.addresses.append(str(want))
        self._record()
        return True

    def ensure(self) -> bool:
        """网线拔插后 NetworkManager 可能切回默认连接，直连地址会丢失；这里补回来。

        返回是否重新配置了。
        """
        if not self.primary:
            return False
        present = {str(ipaddress.ip_interface(a)) for a in iface_addresses(self.iface)["ipv4"]}
        if self.primary in present:
            return False
        if self.backend == "networkmanager":
            run(["nmcli", "--wait", "15", "connection", "up", self.profile, "ifname", self.iface],
                timeout=30)
        else:
            run(["ip", "link", "set", self.iface, "up"])
            run(["ip", "addr", "add", self.primary, "dev", self.iface])
        for cidr in self.addresses:
            if cidr not in present:
                run(["ip", "addr", "add", cidr, "dev", self.iface])
        return True

    def del_address(self, cidr: str) -> None:
        run(["ip", "addr", "del", cidr, "dev", self.iface])
        if cidr in self.addresses:
            self.addresses.remove(cidr)
            self._record()

    def _ufw_allow(self) -> None:
        if ufw_active():
            rule = ["allow", "in", "on", self.iface, "proto", "udp", "to", "any", "port", "67",
                    "comment", "AutoRustDesk-DHCP"]
            rc, out = run(["ufw", "insert", "1"] + rule)
            if rc != 0:
                # 没有任何规则时 insert 1 会报 Invalid position，改为直接添加
                rc, out = run(["ufw"] + rule)
            if rc == 0:
                self.ufw_rule = True
                self.log("ufw 已临时放行 %s 上的 DHCP 请求" % self.iface)
            else:
                self.log("警告：ufw 放行 DHCP 失败：%s" % out.strip())

    # ------------------------------------------------------------------ 恢复

    def down(self) -> None:
        restore(self.iface, {
            "backend": self.backend, "profile": self.profile, "addresses": self.addresses,
            "ufw_rule": self.ufw_rule, "ipv6_was_disabled": self.ipv6_was_disabled,
        }, self.log)
        self.addresses = []
        self.ufw_rule = False
        self._forget()


def restore(iface: str, rec: Dict, log: LogFn) -> None:
    for cidr in rec.get("addresses") or []:
        run(["ip", "addr", "del", cidr, "dev", iface])
    if rec.get("backend") == "networkmanager" and shutil.which("nmcli"):
        run(["nmcli", "connection", "down", rec.get("profile", "")], timeout=30)
        run(["nmcli", "connection", "delete", rec.get("profile", "")], timeout=30)
    if rec.get("ufw_rule") and shutil.which("ufw"):
        run(["ufw", "delete", "allow", "in", "on", iface, "proto", "udp", "to", "any", "port", "67"])
    if rec.get("ipv6_was_disabled") == "1":
        try:
            with open("/proc/sys/net/ipv6/conf/%s/disable_ipv6" % iface, "w") as f:
                f.write("1")
        except OSError:
            pass
    log("已恢复网卡 %s 的原有设置" % iface)


def cleanup_all(log: LogFn) -> List[str]:
    """清理上次异常退出遗留的配置。"""
    state = _load_state()
    done = []
    for iface, rec in list(state.items()):
        restore(iface, rec, log)
        done.append(iface)
    if nm_running():
        rc, out = run(["nmcli", "-t", "-f", "NAME", "connection", "show"])
        if rc == 0:
            for name in out.splitlines():
                if name.startswith(PROFILE_PREFIX):
                    run(["nmcli", "connection", "delete", name])
                    done.append(name)
    _save_state({})
    return done
