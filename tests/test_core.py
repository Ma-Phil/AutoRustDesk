import ipaddress
import types

import pytest

from autorustdesk.core.devices import DeviceRegistry, generate_password
from autorustdesk.core.settings import Settings, choose_subnet
from autorustdesk.core import workflow as W
from autorustdesk.core.workflow import Candidate, Discovery, Ui, Workflow, alias_for


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORUSTDESK_CONFIG_DIR", str(tmp_path / "cfg"))


def test_choose_subnet_avoids_conflicts():
    assert str(choose_subnet("192.168.77.0/24", ["10.0.0.5/8"])) == "192.168.77.0/24"
    assert str(choose_subnet("192.168.77.0/24", ["192.168.77.20/24"])) == "192.168.78.0/24"
    assert str(choose_subnet("192.168.77.0/24", ["192.168.0.0/16"])) == "10.77.77.0/24"


def test_settings_pool_and_validate():
    s = Settings()
    assert s.pool() == ["192.168.77.100", "192.168.77.199"]
    assert str(s.a_address()) == "192.168.77.1/24"
    small = ipaddress.ip_network("10.1.2.0/28")
    start, end = s.pool(small)
    assert ipaddress.ip_address(start) in small and ipaddress.ip_address(end) in small
    assert s.validate() == []
    s.subnet = "8.8.8.0/24"
    assert s.validate()
    s.subnet = "192.168.77.0/24"
    s.password_mode, s.fixed_password = "fixed", "short"
    assert s.validate()


def test_settings_roundtrip_does_not_store_password_unless_asked():
    s = Settings(ssh_user="robot", ssh_password="secret")
    s.save()
    assert Settings.load().ssh_password == ""
    s.remember_ssh_password = True
    s.save()
    assert Settings.load().ssh_password == "secret"


def test_registry_assign_ip_is_stable_and_unique(tmp_path):
    reg = DeviceRegistry(str(tmp_path / "devices.json"))
    net = ipaddress.ip_network("192.168.77.0/24")
    pool = ["192.168.77.100", "192.168.77.199"]
    a = reg.assign_ip("aa:aa:aa:aa:aa:01", net, pool)
    reg.update("aa:aa:aa:aa:aa:01", ip=a)
    b = reg.assign_ip("aa:aa:aa:aa:aa:02", net, pool)
    assert a == "192.168.77.100" and b == "192.168.77.101"
    assert reg.assign_ip("aa:aa:aa:aa:aa:01", net, pool) == a
    assert reg.reservations(net) == {"aa:aa:aa:aa:aa:01": a}
    reg2 = DeviceRegistry(str(tmp_path / "devices.json"))
    assert reg2.get("AA:AA:AA:AA:AA:01").ip == a


def test_generate_password():
    pw = generate_password()
    assert len(pw) == 12 and pw.isalnum()
    assert any(c.isdigit() for c in pw) and any(c.isalpha() for c in pw)


def test_alias_for():
    assert alias_for("10.9.8.7") == "10.9.8.254/24"
    assert alias_for("192.168.1.254") == "192.168.1.253/24"


def test_discovery_aggregates_events():
    d = Discovery(Ui())
    d.on_event({"event": "dhcp_discover", "mac": "52:54:00:00:00:01", "hostname": "robot-1"})
    d.on_event({"event": "neighbor", "mac": "52:54:00:00:00:01", "ipv4": [], "ipv6ll": ["fe80::1"],
                "hostnames": []})
    d.on_event({"event": "lease", "mac": "52:54:00:00:00:01", "ip": "192.168.77.100", "hostname": ""})
    (c,) = d.snapshot()
    assert c.lease_ip == "192.168.77.100" and c.ipv6ll == {"fe80::1"} and c.hostname == "robot-1"
    assert c.dhcp_client
    d.on_event({"event": "foreign_dhcp", "mac": "00:11:22:33:44:55"})
    assert d.foreign_dhcp


def test_pick_candidate_prefers_the_one_in_subnet():
    wf = Workflow(Settings(), DeviceRegistry(), Ui())
    wf.net = ipaddress.ip_network("192.168.77.0/24")
    a = Candidate("52:54:00:00:00:01", ipv4={"10.0.0.2"})
    b = Candidate("52:54:00:00:00:02", ipv4={"192.168.77.100"})
    assert wf._pick_candidate([a, b]) is b
    assert wf._in_subnet_ip(b) == "192.168.77.100"
    assert wf._in_subnet_ip(a) == ""


MAC_B = "52:54:00:00:00:01"


def quick_workflow(monkeypatch, open_hosts, devices=((MAC_B, "192.168.77.100"),)):
    """快速连接试探用的 Workflow：open_hosts 是能连上 RustDesk 端口的地址。"""
    registry = DeviceRegistry()
    for mac, ip in devices:
        registry.update(mac, ip=ip, rustdesk_password="Secret-123", configured_at="2026-09-30 10:00:00")
    wf = Workflow(Settings(), registry, Ui())
    wf.net = ipaddress.ip_network("192.168.77.0/24")
    monkeypatch.setattr(W, "tcp_open", lambda host, port, timeout=3.0: host in open_hosts)
    clock = {"now": 1000.0}
    monkeypatch.setattr(W, "time", types.SimpleNamespace(time=lambda: clock["now"]))
    monkeypatch.setattr(wf, "_sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    return wf


def test_quick_direct_uses_the_remembered_address(monkeypatch):
    wf = quick_workflow(monkeypatch, {"192.168.77.100"})
    assert wf._quick_direct()
    assert (wf.mac, wf.link_ip, wf.ssh_host) == (MAC_B, "192.168.77.100", "192.168.77.100")
    assert wf.password == "Secret-123"


def test_quick_direct_falls_back_when_the_address_does_not_answer(monkeypatch):
    wf = quick_workflow(monkeypatch, set())
    assert not wf._quick_direct()
    assert wf.mac == ""


def test_quick_direct_ignores_unconfigured_or_out_of_subnet_devices(monkeypatch):
    wf = quick_workflow(monkeypatch, {"10.0.0.5", "192.168.77.101"},
                        devices=((MAC_B, "10.0.0.5"), ("52:54:00:00:00:02", "192.168.77.101")))
    wf.registry.update("52:54:00:00:00:02", configured_at="")  # 还没配置过，不能快速连接
    assert not wf._quick_direct()


def test_quick_direct_rejects_address_now_used_by_another_device(monkeypatch):
    wf = quick_workflow(monkeypatch, {"192.168.77.100"})
    wf.discovery.on_event({"event": "lease", "mac": "52:54:00:00:00:99", "ip": "192.168.77.100",
                           "hostname": ""})
    assert not wf._quick_direct()
    assert wf.mac == ""
    # 看到的就是同一台设备时照常连接
    wf.discovery.reset()
    wf.discovery.on_event({"event": "neighbor", "mac": MAC_B.upper(), "ipv4": ["192.168.77.100"],
                           "ipv6ll": [], "hostnames": []})
    assert wf._quick_direct()


def test_quick_direct_gives_up_when_several_known_addresses_answer(monkeypatch):
    wf = quick_workflow(monkeypatch, {"192.168.77.100", "192.168.77.101"},
                        devices=((MAC_B, "192.168.77.100"), ("52:54:00:00:00:02", "192.168.77.101")))
    assert not wf._quick_direct()


def test_quick_run_skips_discovery_when_remembered_address_answers(monkeypatch):
    wf = quick_workflow(monkeypatch, {"192.168.77.100"})
    steps = {}

    class Rec(Ui):
        def step(self, step_id, state, detail=""):
            steps[step_id] = state

    wf.ui = Rec()
    monkeypatch.setattr(wf, "step_network", lambda: "ok")
    monkeypatch.setattr(wf, "step_discover", lambda: pytest.fail("不应该再发现 B"))
    monkeypatch.setattr(wf, "step_verify", lambda: "ok")
    monkeypatch.setattr(wf, "step_connect", lambda: "ok")
    res = wf.run("quick")
    assert res == {"ok": True, "mac": MAC_B, "ip": "192.168.77.100", "password": "Secret-123"}
    assert steps["discover"] == W.SKIPPED
    assert all(steps[k] == W.SKIPPED for k in ("login", "probe", "install", "configure"))


def test_quick_run_falls_back_to_discovery(monkeypatch):
    wf = quick_workflow(monkeypatch, set())
    called = []
    monkeypatch.setattr(wf, "step_network", lambda: "ok")
    monkeypatch.setattr(wf, "step_discover", lambda: called.append("discover") or "ok")
    monkeypatch.setattr(wf, "step_verify", lambda: "ok")
    monkeypatch.setattr(wf, "step_connect", lambda: "ok")
    wf.mac = ""
    # 发现步骤什么都没发现：没有设备信息，快速连接转入完整流程的登录步骤
    monkeypatch.setattr(wf, "step_login", lambda: called.append("login") or "ok")
    for name in ("probe", "install", "configure"):
        monkeypatch.setattr(wf, "step_" + name, lambda n=name: called.append(n) or "ok")
    wf.run("quick")
    assert called == ["discover", "login", "probe", "install", "configure"]
