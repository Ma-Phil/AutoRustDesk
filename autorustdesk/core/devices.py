"""设备记录：按 MAC 区分每一台电脑 B（~/.config/autorustdesk/devices.json）。"""

import dataclasses
import ipaddress
import json
import os
import secrets
import string
import threading
import time
from typing import Dict, List, Optional

from .paths import config_dir, write_private


@dataclasses.dataclass
class Device:
    mac: str
    name: str = ""
    hostname: str = ""
    ip: str = ""
    ssh_user: str = ""
    ssh_password: str = ""
    host_key: str = ""
    rustdesk_password: str = ""
    rustdesk_version: str = ""
    rustdesk_id: str = ""
    configured_at: str = ""
    last_seen: str = ""

    @property
    def title(self) -> str:
        return self.name or self.hostname or self.mac


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def generate_password(length: int = 12) -> str:
    """RustDesk 固定密码：只用字母数字，避免命令行转义问题。"""
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in pw) and any(c.isalpha() for c in pw):
            return pw


class DeviceRegistry:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(config_dir(), "devices.json")
        self._lock = threading.Lock()
        self.devices: Dict[str, Device] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        known = {f.name for f in dataclasses.fields(Device)}
        self.devices = {
            mac: Device(**{k: v for k, v in d.items() if k in known})
            for mac, d in data.get("devices", {}).items()
        }

    def save(self) -> None:
        with self._lock:
            data = {"devices": {m: dataclasses.asdict(d) for m, d in self.devices.items()}}
            write_private(self.path, json.dumps(data, ensure_ascii=False, indent=2))

    def get(self, mac: str) -> Optional[Device]:
        return self.devices.get(mac.lower())

    def get_or_create(self, mac: str) -> Device:
        mac = mac.lower()
        if mac not in self.devices:
            self.devices[mac] = Device(mac=mac)
        return self.devices[mac]

    def update(self, mac: str, **fields) -> Device:
        dev = self.get_or_create(mac)
        for k, v in fields.items():
            setattr(dev, k, v)
        self.save()
        return dev

    def remove(self, mac: str) -> None:
        self.devices.pop(mac.lower(), None)
        self.save()

    def all(self) -> List[Device]:
        return sorted(self.devices.values(), key=lambda d: d.last_seen, reverse=True)

    def reservations(self, network: ipaddress.IPv4Network) -> Dict[str, str]:
        """已知设备固定分到同一个地址（避免同型号设备轮流使用同一 IP 造成混淆）。"""
        result = {}
        for d in self.devices.values():
            try:
                if d.ip and ipaddress.ip_address(d.ip) in network:
                    result[d.mac] = d.ip
            except ValueError:
                continue
        return result

    def assign_ip(self, mac: str, network: ipaddress.IPv4Network, pool: List[str]) -> str:
        """给设备分配（或沿用）直连网段里的地址。"""
        dev = self.get(mac)
        if dev and dev.ip:
            try:
                if ipaddress.ip_address(dev.ip) in network:
                    return dev.ip
            except ValueError:
                pass
        taken = {d.ip for d in self.devices.values() if d.mac != mac.lower()}
        start, end = ipaddress.ip_address(pool[0]), ipaddress.ip_address(pool[1])
        a = start
        while a <= end:
            if str(a) not in taken:
                return str(a)
            a += 1
        return str(end)
