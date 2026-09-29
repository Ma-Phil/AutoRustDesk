"""用网络命名空间模拟"两台电脑用网线直连"：A 在当前命名空间，B 在独立命名空间。"""

import os
import shutil
import subprocess
import time


def sh(*cmd, check=True, timeout=30):
    p = subprocess.run(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    if check and p.returncode != 0:
        raise RuntimeError("%s 失败：%s" % (" ".join(cmd), out))
    return out


def available() -> bool:
    if os.geteuid() != 0 or not shutil.which("ip"):
        return False
    try:
        sh("ip", "netns", "add", "ard-probe")
        sh("ip", "netns", "del", "ard-probe")
        return True
    except Exception:  # noqa: BLE001
        return False


class DirectLink:
    """A 端网卡 a_if 在当前命名空间；B 端网卡 b_if 在命名空间 ns。"""

    def __init__(self, ns="ard-b", a_if="ardA0", b_if="ardB0"):
        self.ns = ns
        self.a_if = a_if
        self.b_if = b_if

    def __enter__(self):
        self.destroy()
        sh("ip", "netns", "add", self.ns)
        sh("ip", "link", "add", self.a_if, "type", "veth", "peer", "name", self.b_if)
        sh("ip", "link", "set", self.b_if, "netns", self.ns)
        self.b("ip", "link", "set", "lo", "up")
        self.b("ip", "link", "set", self.b_if, "up")
        sh("ip", "link", "set", self.a_if, "up")
        time.sleep(0.5)
        return self

    def b(self, *cmd, check=True, timeout=30):
        return sh("ip", "netns", "exec", self.ns, *cmd, check=check, timeout=timeout)

    def b_popen(self, *cmd):
        return subprocess.Popen(["ip", "netns", "exec", self.ns] + list(cmd),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def b_mac(self):
        out = self.b("cat", "/sys/class/net/%s/address" % self.b_if)
        return out.strip()

    def destroy(self):
        sh("ip", "link", "del", self.a_if, check=False)
        sh("ip", "netns", "del", self.ns, check=False)

    def __exit__(self, *exc):
        self.destroy()
