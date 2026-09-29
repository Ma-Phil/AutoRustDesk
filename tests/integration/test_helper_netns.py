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
DHCLIENT_PID = "/run/dhclient-ard-test.pid"
DHCLIENT_LEASES = "/var/lib/dhcp/dhclient.ard-test.leases"


def ipv6_supported():
    import socket

    try:
        socket.socket(socket.AF_INET6, socket.SOCK_DGRAM).close()
        return True
    except OSError:
        return False


def pcap_available():
    from autorustdesk.helper.packetio import libpcap

    return libpcap() is not None


# afpacket：Linux 的默认实现；pcap：macOS/Windows 用的 libpcap 实现；
# none：不能抓包（模拟 Windows 没装 Npcap），靠 DHCP 记录、系统邻居表发现 B
CAPTURE_MODES = [
    "afpacket",
    pytest.param("pcap", marks=pytest.mark.skipif(not pcap_available(), reason="需要 libpcap")),
    "none",
]


class HelperProc:
    def __init__(self, packetio="afpacket", transport="stdio"):
        from autorustdesk.helper.transport import Listener

        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}  # 模拟 pkexec 的干净环境
        if packetio != "afpacket":
            env["AUTORUSTDESK_PACKETIO"] = packetio
        cmd = [sys.executable, os.path.join(ROOT, "autorustdesk", "helper", "__main__.py")]
        self.listener = None
        if transport == "tcp":
            # macOS/Windows 的方式：助手连回界面监听的本机端口
            self.listener = Listener()
            cmd += ["--connect", self.listener.address, "--token-file", self.listener.token_file]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env)
        self.q = queue.Queue()
        self.events = []
        self.responses = {}
        self.next_id = 0
        if self.listener:
            self.conn, reader = self.listener.accept(20, lambda: self.p.poll() is None)
        else:
            reader = self.p.stdout
        threading.Thread(target=self._reader, args=(reader,), daemon=True).start()
        msg = self.q.get(timeout=10)
        assert msg["type"] == "ready", msg

    def _reader(self, reader):
        for line in reader:
            msg = json.loads(line)
            if msg["type"] == "response":
                self.responses[msg["id"]] = msg
            elif msg["type"] == "event":
                self.events.append(msg)
            self.q.put(msg)

    def _write(self, data):
        if self.listener:
            self.conn.sendall(data)
        else:
            self.p.stdin.write(data)
            self.p.stdin.flush()

    def call(self, cmd, timeout=30, **kw):
        self.next_id += 1
        rid = self.next_id
        self._write((json.dumps(dict(kw, id=rid, cmd=cmd)) + "\n").encode())
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
        if self.listener:
            import socket

            self.conn.shutdown(socket.SHUT_WR)
            self.p.wait(timeout=10)
            self.conn.close()
            self.listener.close()
        else:
            self.p.stdin.close()
            self.p.wait(timeout=10)


def a_addresses(iface):
    out = subprocess.run(["ip", "-j", "addr", "show", "dev", iface], stdout=subprocess.PIPE).stdout
    return [a["local"] for item in json.loads(out) for a in item["addr_info"] if a["family"] == "inet"]


@pytest.mark.skipif(not shutil.which("dhclient"), reason="需要 isc-dhcp-client")
@pytest.mark.parametrize("packetio", CAPTURE_MODES)
def test_dhcp_lease_and_ipv6_discovery(packetio):
    with DirectLink() as link:
        h = HelperProc(packetio)
        try:
            r = h.call("link_up", iface=link.a_if, cidr="192.168.77.1/24")
            assert r["ok"], r
            assert r["result"]["backend"] == "iproute2"
            assert r["result"]["scope"] == link.a_if
            assert r["result"]["capture"] == {"none": "none"}.get(packetio, packetio)
            assert "192.168.77.1" in a_addresses(link.a_if)
            r = h.call("sniff_start", iface=link.a_if)
            assert r["ok"] and r["result"]["capture"] == {"none": "none"}.get(packetio, packetio), r
            r = h.call("dhcp_start", iface=link.a_if, server_ip="192.168.77.1", prefix=24,
                       pool_start="192.168.77.100", pool_end="192.168.77.199", lease_time=600)
            assert r["ok"], r

            b_mac = link.b_mac()
            # 用 AppArmor 的 dhclient 配置允许的路径（Ubuntu 上 /tmp 下的文件会被拒绝）
            os.makedirs("/var/lib/dhcp", exist_ok=True)
            dh = link.b_popen("dhclient", "-d", "-v", "-1", "-pf", DHCLIENT_PID,
                              "-lf", DHCLIENT_LEASES, link.b_if)
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
                if ipv6_supported() and packetio != "none":
                    # 发出即返回，B 的回应由链路监听上报
                    assert r["ok"] and r["result"]["sent"] == 2, r
                    neigh = h.wait_event(lambda e: e["event"] == "neighbor" and e["ipv6ll"], timeout=10)
                    assert neigh["mac"] == b_mac
                elif not ipv6_supported():
                    # 内核不支持 IPv6 时只返回错误，不影响助手继续工作
                    assert not r["ok"]
                    assert h.call("leases")["ok"]
                neigh = h.wait_event(lambda e: e["event"] == "neighbor", timeout=10)
                assert neigh["mac"] == b_mac
            finally:
                dh.kill()
                link.b("dhclient", "-x", "-pf", DHCLIENT_PID, check=False)
                for f in (DHCLIENT_PID, DHCLIENT_LEASES):
                    if os.path.exists(f):
                        os.remove(f)
        finally:
            h.close()
        # 助手退出后 A 的网卡恢复原状
        assert "192.168.77.1" not in a_addresses(link.a_if)


@pytest.mark.parametrize("packetio", CAPTURE_MODES[:2])
def test_static_b_is_seen_by_sniffer_and_foreign_dhcp_blocks(packetio):
    with DirectLink() as link:
        link.b("ip", "addr", "add", "10.9.8.7/24", "dev", link.b_if)
        h = HelperProc(packetio)
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


@pytest.mark.parametrize("packetio", CAPTURE_MODES)
def test_arp_scan_finds_silent_static_b(packetio):
    """B 有固定地址但一声不吭：主动 ARP 扫描把它找出来。"""
    with DirectLink() as link:
        link.b("ip", "addr", "add", "192.168.77.50/24", "dev", link.b_if)
        link.b("ip", "addr", "add", "10.0.0.9/24", "dev", link.b_if)
        b_mac = link.b_mac()
        h = HelperProc(packetio)
        try:
            assert h.call("link_up", iface=link.a_if, cidr="192.168.77.1/24")["ok"]
            assert h.call("sniff_start", iface=link.a_if)["ok"]
            r = h.call("arp_scan", iface=link.a_if, targets=["192.168.77.0/24"], timeout=60)
            assert r["ok"], r
            if packetio == "none":
                # 只能用系统接口逐个解析本网段地址，在后台进行
                assert r["result"]["sent"] == 253
            neigh = h.wait_event(lambda e: e["event"] == "neighbor" and "192.168.77.50" in e["ipv4"],
                                 timeout=60)
            assert neigh["mac"] == b_mac
            r = h.call("arp_scan", iface=link.a_if, common=True, pps=4000, timeout=60)
            assert r["ok"], r
            if packetio == "none":
                # 不能发 ARP 探测，常见网段扫描做不了，但不报错
                assert r["result"]["sent"] == 0
            else:
                # 不在本机网段的地址用 ARP 探测（发送方 0.0.0.0），B 同样回应
                h.wait_event(lambda e: e["event"] == "neighbor" and "10.0.0.9" in e["ipv4"], timeout=10)
        finally:
            h.close()


def test_connect_back_transport():
    """macOS/Windows 的通信方式：助手连回界面监听的端口，双方用令牌验证；断开后助手恢复网卡并退出。"""
    with DirectLink() as link:
        h = HelperProc(transport="tcp")
        r = h.call("hello")
        assert r["ok"] and r["result"]["admin"] is True, r
        assert h.call("link_up", iface=link.a_if, cidr="192.168.77.1/24")["ok"]
        assert "192.168.77.1" in a_addresses(link.a_if)
        h.close()
        assert h.p.returncode == 0
        assert "192.168.77.1" not in a_addresses(link.a_if)


def test_connect_back_rejects_wrong_token(tmp_path):
    from autorustdesk.helper.transport import Listener

    listener = Listener()
    bad = tmp_path / "token"
    bad.write_text("0" * 64)
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "autorustdesk", "helper", "__main__.py"),
                          "--connect", listener.address, "--token-file", str(bad)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    try:
        with pytest.raises(Exception):
            listener.accept(5, lambda: p.poll() is None)
        assert p.wait(timeout=10) == 2  # 助手发现界面验证不通过，不执行任何命令就退出
    finally:
        p.kill()
        listener.close()
