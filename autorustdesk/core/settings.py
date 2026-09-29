"""程序设置（保存在 ~/.config/autorustdesk/settings.json，仅当前用户可读）。"""

import dataclasses
import ipaddress
import json
import os
from typing import Any, Dict, List, Optional

from .paths import config_dir, write_private

# 当首选直连网段与本机其它网络冲突时依次尝试
FALLBACK_SUBNETS = ["192.168.77.0/24", "192.168.78.0/24", "10.77.77.0/24", "172.31.77.0/24"]


@dataclasses.dataclass
class Settings:
    iface: str = ""
    subnet: str = "192.168.77.0/24"
    lease_time: int = 3600
    bundle_path: str = ""
    rustdesk_port: int = 21118
    whitelist: bool = True
    headless: str = "auto"  # auto / on / off / skip
    resolution: str = "1920x1080"
    prevent_sleep: bool = True
    password_mode: str = "per_device"  # per_device / fixed
    fixed_password: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    remember_ssh_password: bool = False
    ssh_password: str = ""
    ssh_key_file: str = ""
    rustdesk_client: str = ""
    discover_timeout: int = 180
    show_virtual_nics: bool = False
    mirror: str = ""
    auto_launch: bool = True

    # ---------------------------------------------------------------- 派生值

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_network(self.subnet, strict=False)

    def a_address(self, network: Optional[ipaddress.IPv4Network] = None) -> ipaddress.IPv4Interface:
        net = network or self.network
        return ipaddress.ip_interface("%s/%d" % (net.network_address + 1, net.prefixlen))

    def pool(self, network: Optional[ipaddress.IPv4Network] = None) -> List[str]:
        """DHCP 地址池：.100 - .199（小网段时按比例缩小）。"""
        net = network or self.network
        hosts = net.num_addresses - 2
        if hosts >= 254:
            return [str(net.network_address + 100), str(net.network_address + 199)]
        start = max(2, hosts // 2)
        return [str(net.network_address + start), str(net.network_address + hosts)]

    def validate(self) -> List[str]:
        errors = []
        try:
            net = self.network
            if not net.is_private:
                errors.append("直连网段必须是私有地址")
            if net.prefixlen > 29:
                errors.append("直连网段太小")
        except ValueError:
            errors.append("直连网段格式不对：%s" % self.subnet)
        if not (1 <= self.rustdesk_port <= 65535):
            errors.append("RustDesk 端口不对")
        if self.headless not in ("auto", "on", "off", "skip"):
            errors.append("虚拟显示器模式不对")
        if self.password_mode == "fixed" and len(self.fixed_password) < 8:
            errors.append("统一密码至少 8 位")
        return errors

    # ---------------------------------------------------------------- 读写

    @classmethod
    def path(cls) -> str:
        return os.path.join(config_dir(), "settings.json")

    @classmethod
    def load(cls) -> "Settings":
        try:
            with open(cls.path(), "r", encoding="utf-8") as f:
                data: Dict[str, Any] = json.load(f)
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        data = dataclasses.asdict(self)
        if not self.remember_ssh_password:
            data["ssh_password"] = ""
        write_private(self.path(), json.dumps(data, ensure_ascii=False, indent=2))


def choose_subnet(preferred: str, used: List[str]) -> Optional[ipaddress.IPv4Network]:
    """从首选网段和备选网段中挑一个不与本机已有网络冲突的。"""
    used_nets = []
    for u in used:
        try:
            used_nets.append(ipaddress.ip_network(u, strict=False))
        except ValueError:
            continue
    for cand in [preferred] + [s for s in FALLBACK_SUBNETS if s != preferred]:
        net = ipaddress.ip_network(cand, strict=False)
        if not any(net.overlaps(u) for u in used_nets):
            return net
    return None
