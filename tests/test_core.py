import ipaddress

import pytest

from autorustdesk.core.devices import DeviceRegistry, generate_password
from autorustdesk.core.settings import Settings, choose_subnet
from autorustdesk.core.workflow import Candidate, Discovery, Ui, Workflow, alias_for


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))


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
