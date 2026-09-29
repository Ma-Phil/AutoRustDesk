"""端到端测试用的假 RustDesk（tests/fakes/fake_rustdesk.py）真的跑起来：

它要和真实的 RustDesk 一样，在 --server 起来之前写的设置会丢；
ard_remote.configure_rustdesk 对着它也要能把直连端口打开。
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

from autorustdesk.remote import ard_remote as R

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="读 /proc，只在 Linux 上测")

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes", "fake_rustdesk.py")
PASSWORD = "Secret-123"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    exe = tmp_path / "rustdesk"
    shutil.copy(FAKE, str(exe))
    exe.chmod(0o755)
    state = tmp_path / "state"
    monkeypatch.setenv("FAKE_RUSTDESK_STATE", str(state))
    monkeypatch.setenv("FAKE_RUSTDESK_ASSUME_ROOT", "1")
    monkeypatch.setattr(R, "rustdesk_bin", lambda: str(exe))
    procs = []

    def start():
        with open(str(tmp_path / "fake.log"), "ab") as log:
            procs.append(subprocess.Popen([str(exe), "--service"], stdout=log, stderr=log,
                                          start_new_session=True))

    def stop():
        while procs:
            p = procs.pop()
            try:
                os.killpg(p.pid, signal.SIGKILL)  # 连同 --server 子进程
            except OSError:
                pass
            p.wait()

    def systemctl(*args, timeout=60):
        if args[:2] == ("restart", "rustdesk"):
            stop()
            start()
        return 0, ""

    monkeypatch.setattr(R, "systemctl", systemctl)
    yield start, state
    stop()


def root_options(state):
    with open(str(state / "options.json")) as f:
        return json.load(f)


def test_fake_loses_settings_written_before_server_starts(fake):
    start, state = fake
    port = free_port()
    start()
    deadline = time.time() + 10
    while not (state / "service.json").exists():  # --service 已经读过配置文件
        assert time.time() < deadline
        time.sleep(0.05)
    R.rd(["--option", "direct-server", "Y"])
    R.rd(["--option", "direct-access-port", str(port)])
    assert root_options(state)["direct-server"] == "Y"  # 只写进了配置文件
    ok, _msg = R.rd_set_password(PASSWORD, timeout=20)  # 要等 --server 起来
    assert ok
    time.sleep(1)
    assert "direct-server" not in root_options(state)
    assert port not in R.listen_ports()


def test_configure_rustdesk_opens_port_on_fake(fake, monkeypatch):
    start, state = fake
    monkeypatch.setattr(R, "SERVER_SETTLE_SECONDS", 1)
    port = free_port()
    start()
    a = R.build_parser().parse_args(
        ["configure", "--password-file", "x", "--port", str(port), "--whitelist", "192.168.77.0/24"])
    changes = []
    _rid, listening, servers = R.configure_rustdesk(a, PASSWORD, changes, xorg_ok=True)
    assert listening and len(servers) == 1 and len(changes) == 5
    assert root_options(state) == {
        "direct-server": "Y", "direct-access-port": str(port),
        "verification-method": "use-permanent-password", "approve-mode": "password",
        "whitelist": "192.168.77.0/24"}
    assert (state / "password.txt").read_text() == PASSWORD
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        assert s.recv(64).startswith(b"FAKE-RUSTDESK")
