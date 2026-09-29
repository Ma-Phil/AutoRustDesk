"""网络助手的真实网络测试（需要 root 和网络命名空间）。

    sudo python3 -m pytest tests/integration/test_helper_netns.py
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

import pytest

from tests.integration.netns import DirectLink, available

pytestmark = pytest.mark.skipif(not available(), reason="需要 root 和网络命名空间")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def ipv6_supported():
    import socket

    try:
        socket.socket(socket.AF_INET6, socket.SOCK_DGRAM).close()
        return True
    except OSError:
        return False


class HelperProc:
    def __init__(self):
        self.p = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "autorustdesk", "helper", "__main__.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},  # 模拟 pkexec 的干净环境
        )
        self.q = queue.Queue()
        self.events = []
        self.responses = {}
        self.next_id = 0
        threading.Thread(target=self._reader, daemon=True).start()
        msg = self.q.get(timeout=10)
        assert msg["type"] == "ready", msg

    def _reader(self):
        for line in self.p.stdout:
            msg = json.loads(line)
            if msg["type"] == "response":
                self.responses[msg["id"]] = msg
            elif msg["type"] == "event":
                self.events.append(msg)
            self.q.put(msg)

    def call(self, cmd, timeout=30, **kw):
        self.next_id += 1
        rid = self.next_id
        self.p.stdin.write((json.dumps(dict(kw, id=rid, cmd=cmd)) + "\n").encode())
        self.p.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if rid in self.responses:
                return self.responses.pop(rid)
            time.sleep(0.05)
        raise TimeoutError(cmd)

    def wait_event(self, pred, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for e in list(self.events):
                if pred(e):
                    return e
            time.sleep(0.1)
        raise TimeoutError("没有等到事件；已有事件：%s" % self.events)

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


def a_addresses(iface):
    out = subprocess.run(["ip", "-j", "addr", "show", "dev", iface], stdout=subprocess.PIPE).stdout
    return [a["local"] for item in json.loads(out) for a in item["addr_info"] if a["family"] == "inet"]


@pytest.mark.skipif(not shutil.which("dhclient"), reason="需要 isc-dhcp-client")
def test_dhcp_lease_and_ipv6_discovery():
    with DirectLink() as link:
        h = HelperProc()
        try:
            r = h.call("link_up", iface=link.a_if, cidr="192.168.77.1/24")
            assert r["ok"], r
            assert r["result"]["backend"] == "iproute2"
            assert "192.168.77.1" in a_addresses(link.a_if)
            assert h.call("sniff_start", iface=link.a_if)["ok"]
            r = h.call("dhcp_start", iface=link.a_if, server_ip="192.168.77.1", prefix=24,
                       pool_start="192.168.77.100", pool_end="192.168.77.199", lease_time=600)
            assert r["ok"], r

            b_mac = link.b_mac()
            dh = link.b_popen("dhclient", "-d", "-v", "-1", "-pf", "/tmp/ard-dhclient.pid",
                              "-lf", "/tmp/ard-dhclient.leases", link.b_if)
            try:
                lease = h.wait_event(lambda e: e["event"] == "lease", timeout=40)
                assert lease["mac"] == b_mac
                assert lease["ip"] == "192.168.77.100"
                # B 真的拿到了地址，而且没有默认路由指向 A
                deadline = time.time() + 10
                while "192.168.77.100" not in link.b("ip", "addr") and time.time() < deadline:
                    time.sleep(0.5)
                assert "192.168.77.100" in link.b("ip", "addr")
                assert "default" not in link.b("ip", "route")
                subprocess.run(["ping", "-c", "1", "-W", "2", "192.168.77.100"], check=True,
                               stdout=subprocess.DEVNULL)

                r = h.call("probe6", iface=link.a_if, timeout=2)
                if ipv6_supported():
                    # 发出即返回，B 的回应由链路监听上报
                    assert r["ok"] and r["result"]["sent"] == 2, r
                    neigh = h.wait_event(lambda e: e["event"] == "neighbor" and e["ipv6ll"], timeout=10)
                    assert neigh["mac"] == b_mac
                else:
                    # 内核不支持 IPv6 时只返回错误，不影响助手继续工作
                    assert not r["ok"]
                    assert h.call("leases")["ok"]
                neigh = h.wait_event(lambda e: e["event"] == "neighbor", timeout=10)
                assert neigh["mac"] == b_mac
            finally:
                dh.kill()
                link.b("dhclient", "-x", "-pf", "/tmp/ard-dhclient.pid", check=False)
        finally:
            h.close()
        # 助手退出后 A 的网卡恢复原状
        assert "192.168.77.1" not in a_addresses(link.a_if)


def test_static_b_is_seen_by_sniffer_and_foreign_dhcp_blocks():
    with DirectLink() as link:
        link.b("ip", "addr", "add", "10.9.8.7/24", "dev", link.b_if)
        h = HelperProc()
        try:
            assert h.call("link_up", iface=link.a_if, cidr="192.168.77.1/24")["ok"]
            assert h.call("sniff_start", iface=link.a_if)["ok"]
            # B 有固定 IP，访问它的网关时会发 ARP，暴露自己的地址
            link.b("ping", "-c", "1", "-W", "1", "10.9.8.1", check=False)
            neigh = h.wait_event(lambda e: e["event"] == "neighbor" and e["ipv4"], timeout=10)
            assert neigh["ipv4"] == ["10.9.8.7"]
            # A 临时加一个 B 网段的地址后就能直接访问 B
            assert h.call("add_address", iface=link.a_if, cidr="10.9.8.254/24")["ok"]
            subprocess.run(["ping", "-c", "1", "-W", "2", "10.9.8.7"], check=True,
                           stdout=subprocess.DEVNULL)

            # 模拟插错网线：链路上出现别的 DHCP 服务器的回复
            link.b("python3", "-c", (
                "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
                "s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1);s.bind(('10.9.8.7',67));"
                "s.sendto(b'x'*300,('255.255.255.255',68))"))
            h.wait_event(lambda e: e["event"] == "foreign_dhcp", timeout=10)
            r = h.call("dhcp_start", iface=link.a_if, server_ip="192.168.77.1", prefix=24,
                       pool_start="192.168.77.100", pool_end="192.168.77.199")
            assert not r["ok"] and "DHCP" in r["error"]
        finally:
            h.close()
        assert a_addresses(link.a_if) == []


def test_restore_network_cleans_up_after_crash(tmp_path, monkeypatch):
    """上次异常退出遗留了地址和记录：点「恢复本机网络」应当清理干净。"""
    from autorustdesk.core.devices import DeviceRegistry
    from autorustdesk.core.settings import Settings
    from autorustdesk.core.workflow import Ui, Workflow, stale_network_config
    from autorustdesk.helper import linkconfig

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with DirectLink() as link:
        subprocess.run(["ip", "addr", "add", "192.168.77.1/24", "dev", link.a_if], check=True)
        os.makedirs(linkconfig.STATE_DIR, exist_ok=True)
        with open(linkconfig.STATE_FILE, "w") as f:
            json.dump({link.a_if: {"backend": "iproute2", "addresses": ["192.168.77.1/24"]}}, f)
        assert link.a_if in stale_network_config()
        wf = Workflow(Settings(), DeviceRegistry(str(tmp_path / "d.json")), Ui())
        wf.restore_network()
        assert "192.168.77.1" not in a_addresses(link.a_if)
        assert stale_network_config() == []
        assert not wf.helper.running


def test_bounce_link_makes_b_see_cable_replug():
    """电子拔插：B 端能看到网线断开再接上，A 的直连配置保持不变，也不误报断线。"""
    with DirectLink() as link:
        h = HelperProc()
        try:
            assert h.call("link_up", iface=link.a_if, cidr="192.168.77.1/24")["ok"]
            before = int(link.b("cat", "/sys/class/net/%s/carrier_changes" % link.b_if))
            t0 = time.time()
            r = h.call("bounce_link", iface=link.a_if, mode="quick", timeout=60)
            assert r["ok"], r
            # veth 不支持重新协商，退回到关闭网口 2 秒
            assert "关闭网口" in r["result"]["method"]
            assert time.time() - t0 < 15
            after = int(link.b("cat", "/sys/class/net/%s/carrier_changes" % link.b_if))
            assert after - before >= 2, (before, after)
            assert link.b("cat", "/sys/class/net/%s/carrier" % link.b_if).strip() == "1"
            assert "192.168.77.1" in a_addresses(link.a_if)
            time.sleep(2)
            assert not [e for e in h.events if e["event"] == "carrier"], h.events
        finally:
            h.close()
