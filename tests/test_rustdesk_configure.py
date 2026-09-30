"""电脑 B 上配置 RustDesk 的流程（ard_remote.configure_rustdesk）。

用一个按 RustDesk 1.4.9 源码写的模型代替真实的 RustDesk，时间是模拟的：
  - root 的 --service 启动时读一次配置文件，之后只在 --server 推送时更新自己的内存并写回文件；
  - --service 启动后过一会儿才以会话用户（登录界面是 gdm）身份启动 --server；
  - --server 启动 0.25 秒后 IPC 可用，0.35 秒时从 --service 同步一次配置（覆盖自己的），
    之后自己的配置一变就推给 --service；以 root 身份运行的 --server 直接读写配置文件，不同步；
  - `rustdesk --option` 连得上 IPC 就改 --server 的，同时总会把结果写进 root 的配置文件；
    `rustdesk --password` 只能通过 IPC。
"""

import types

import pytest

from autorustdesk.remote import ard_remote as R

PORT = 21118
PASSWORD = "Secret-123"
IPC_UP, SYNCED, LISTENS = 0.25, 0.35, 0.4
CALL = 0.15  # 运行一次 rustdesk 命令行的耗时


def cfg(options=None, password=""):
    return {"options": dict(options or {}), "password": password}


def copy(c):
    return cfg(c["options"], c["password"])


class FakeRustDesk:
    def __init__(self, server_delay=2.0, server_uid=126):
        self.now = 1000.0
        self.server_delay = server_delay  # None：没有图形会话，--server 不会启动
        self.server_uid = server_uid
        self.root = cfg()  # root 的配置文件
        self.svc = None  # --service 内存里的配置；None 表示服务没在运行
        self.server = None
        self.server_due = None  # 下一个 --server 什么时候启动
        self.next_pid = 5000
        self.kill_server_at = None  # 模拟 --service 在这一刻重启 --server
        self.restarts = 0
        self.service_dies = False  # RustDesk 服务启动后马上退出（配置里有 stop-service）

    # ------------------------------------------------------------ 状态随时间变化
    def _update(self):
        if self.kill_server_at is not None and self.now >= self.kill_server_at:
            self.kill_server_at = None
            self.server = None
            self.server_due = self.now + 2.0
        if self.server is None and self.server_due is not None and self.now >= self.server_due:
            self.next_pid += 1
            root = self.server_uid == 0
            self.server = {"pid": self.next_pid, "uid": self.server_uid, "start": self.server_due,
                           # root 的 --server 读配置文件；其它用户的先用自己的，等同步
                           "mem": copy(self.root) if root else cfg(), "synced": root}
            self.server_due = None
        s = self.server
        if s and not s["synced"] and self.now >= s["start"] + SYNCED:
            s["mem"] = copy(self.svc)
            s["synced"] = True

    def _ipc_up(self):
        return self.server is not None and self.now >= self.server["start"] + IPC_UP

    def _server_changed(self):
        s = self.server
        if not s["synced"]:
            return  # 马上会被同步覆盖
        if s["uid"] != 0:
            self.svc = copy(s["mem"])
        self.root = copy(s["mem"])

    def service_start(self):
        self.svc = copy(self.root)
        self.server = None
        self.server_due = None if self.server_delay is None else self.now + self.server_delay

    # ------------------------------------------------------------ 给 ard_remote 用的替身
    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def rd(self, args, timeout=30):
        self._update()
        self.now += CALL
        src = self.server["mem"] if self._ipc_up() else self.root
        if args[0] == "--get-id":
            return 0, ["123456789"]  # 连不上 IPC 时读 root 的配置文件，照样有 ID
        if args[0] == "--option" and len(args) == 2:
            value = src["options"].get(args[1], "")
            return 0, [value] if value else []
        if args[0] == "--option":
            opts = dict(src["options"])
            if args[2]:
                opts[args[1]] = args[2]
            else:
                opts.pop(args[1], None)
            if self._ipc_up():
                self.server["mem"]["options"] = dict(opts)
                self._server_changed()
            self.root["options"] = opts  # 命令行进程自己也写 root 的配置文件
            return 0, []
        if args[0] == "--password":
            if not self._ipc_up():
                return 0, ["No such file or directory (os error 2)"]
            self.server["mem"]["password"] = args[1]
            self._server_changed()
            self.root["password"] = args[1]
            return 0, ["Done!"]
        raise AssertionError(args)

    def systemctl(self, *args, timeout=60):
        self._update()
        if args[0] == "is-active":
            running = self.svc is not None and not self.service_dies
            return (0, "active\n") if running else (3, "inactive\n")
        if args[0] == "is-enabled":
            return 0, "enabled\n"
        if args[:2] in (("restart", "rustdesk"), ("start", "rustdesk")):
            self.restarts += args[0] == "restart"
            self.service_start()
        return 0, ""

    def listen_ports(self):
        self._update()
        s = self.server
        if s and s["synced"] and self.now >= s["start"] + LISTENS \
                and s["mem"]["options"].get("direct-server") == "Y":
            return {int(s["mem"]["options"].get("direct-access-port") or PORT)}
        return set()

    def rustdesk_servers(self, proc="/proc"):
        self._update()
        s = self.server
        return [{"pid": s["pid"], "uid": s["uid"], "age": self.now - s["start"]}] if s else []

    # ------------------------------------------------------------ 检查
    def listening_after_reboot(self):
        """模拟 B 重启：设置要存进 root 的配置文件，重启后才还在。"""
        self.service_start()
        self.sleep(10)
        return PORT in self.listen_ports()


WANTED = {"direct-server": "Y", "direct-access-port": str(PORT),
          "verification-method": "use-permanent-password", "approve-mode": "password",
          "whitelist": "192.168.77.0/24"}


@pytest.fixture
def rustdesk(monkeypatch):
    def make(**kw):
        m = FakeRustDesk(**kw)
        monkeypatch.setattr(R, "time", types.SimpleNamespace(time=m.time, sleep=m.sleep))
        monkeypatch.setattr(R, "rd", m.rd)
        monkeypatch.setattr(R, "systemctl", m.systemctl)
        monkeypatch.setattr(R, "listen_ports", m.listen_ports)
        monkeypatch.setattr(R, "rustdesk_servers", m.rustdesk_servers)
        return m
    return make


def args():
    return R.build_parser().parse_args(
        ["configure", "--password-file", "x", "--port", str(PORT), "--whitelist", "192.168.77.0/24"])


def old_configure(a):
    """修复前的做法：`rustdesk --get-id` 在 --server 起来之前也能返回 ID，于是服务一启动就写设置；
    端口没打开就重启服务再等。"""
    assert R.rd_get_id()
    for key, value in R.rustdesk_options(a):
        if R.rd_get_option(key) != value:
            R.rd_set_option(key, value)
    assert R.rd_set_password(PASSWORD)[0]
    if R.wait_listening(PORT, 30):
        return True
    R.systemctl("restart", "rustdesk")
    R.rd_get_id()
    return R.wait_listening(PORT, 30)


def test_model_reproduces_the_failure_seen_on_b(rustdesk):
    # 现场日志：刚启动（或重启）RustDesk 服务就写设置，端口一直没打开，重启服务也没用；
    # 再运行一次（这时 --server 早已稳定）才好。模型要能复现，下面的测试才有意义
    m = rustdesk()
    m.service_start()
    assert not old_configure(args())
    assert "direct-server" not in m.root["options"] and "direct-server" not in m.svc["options"]
    assert old_configure(args())


def test_settings_written_right_after_service_start_take_effect(rustdesk):
    m = rustdesk()
    m.service_start()  # 与日志一样：服务刚启动（或登录界面刚重启）
    changes = []
    rid, listening, servers = R.configure_rustdesk(args(), PASSWORD, changes)
    assert listening and rid == "123456789"
    assert [s["uid"] for s in servers] == [126]
    assert len(changes) == 5 and changes[0] == "RustDesk direct-server：（空） → Y"
    for c in (m.server["mem"], m.svc, m.root):
        assert c["options"] == WANTED and c["password"] == PASSWORD
    assert m.restarts == 0
    assert m.listening_after_reboot()


def test_already_configured_b_is_left_alone(rustdesk):
    m = rustdesk()
    m.root = cfg(WANTED, PASSWORD)
    m.service_start()
    m.sleep(3600)
    changes = []
    _rid, listening, _servers = R.configure_rustdesk(args(), PASSWORD, changes)
    assert listening and changes == [] and m.restarts == 0


def test_settings_rewritten_when_server_restarts_midway(rustdesk, capsys):
    # --service 在写设置的过程中重启了 --server：之后写的几项只进了配置文件，随后被覆盖
    m = rustdesk()
    m.service_start()
    m.sleep(3600)
    m.kill_server_at = m.now + 1.0
    _rid, listening, _servers = R.configure_rustdesk(args(), PASSWORD, [])
    assert listening
    assert m.server["mem"]["options"] == WANTED and m.svc["options"] == WANTED
    assert m.server["mem"]["password"] == PASSWORD and m.svc["password"] == PASSWORD
    assert "重新写入 RustDesk 设置" in capsys.readouterr().out
    assert m.listening_after_reboot()


def test_root_server_restarts_service_so_login_keeps_settings(rustdesk):
    # 登录界面是 Wayland：--server 以 root 身份运行，不和 --service 同步配置
    m = rustdesk(server_uid=0)
    m.service_start()
    _rid, listening, _servers = R.configure_rustdesk(args(), PASSWORD, [])
    assert listening and m.restarts == 1
    assert m.svc["options"] == WANTED and m.svc["password"] == PASSWORD
    # 用户登录桌面：新的 --server 以用户身份运行，从 --service 同步配置
    m.server_uid, m.server, m.server_due = 1000, None, m.now + 2
    m.sleep(10)
    assert PORT in m.listen_ports() and m.server["mem"]["password"] == PASSWORD


def test_no_graphical_session_fails_with_clear_message(rustdesk):
    m = rustdesk(server_delay=None)
    m.service_start()
    start = m.now
    with pytest.raises(R.ArdError, match="没有启动 --server"):
        R.configure_rustdesk(args(), PASSWORD, [])
    assert m.now - start < 200


def test_is_rustdesk_server():
    path = "/usr/share/rustdesk/rustdesk"
    exe = R.os.path.realpath(path)
    assert R.is_rustdesk_server([path, "--server", ""], exe)
    assert R.is_rustdesk_server(["python3", path, "--server"], exe)  # 测试用的假 RustDesk
    assert not R.is_rustdesk_server(
        ["sudo", "-E", "XDG_RUNTIME_DIR=/run/user/1000", "-u", "mz", path, "--server"], exe)
    assert not R.is_rustdesk_server([path, "--service"], exe)
    assert not R.is_rustdesk_server([path, "--tray"], exe)
    assert not R.is_rustdesk_server(["/opt/other/rustdesk", "--server"], exe)
    assert not R.is_rustdesk_server([], exe)


@pytest.mark.skipif(not hasattr(R.os, "sysconf"), reason="只在 Linux 上读 /proc")
def test_rustdesk_servers_reads_proc(tmp_path, monkeypatch):
    exe = tmp_path / "rustdesk"
    exe.write_text("")
    monkeypatch.setattr(R, "rustdesk_bin", lambda: str(exe))
    hz = R.os.sysconf("SC_CLK_TCK")
    procs = {
        "100": ([str(exe), "--service"], 50),
        "200": (["sudo", "-E", "-u", "gdm", str(exe), "--server"], 900),
        "201": ([str(exe), "--server"], 900),
        "300": (["/usr/bin/python3", "x.py"], 10),
    }
    (tmp_path / "uptime").write_text("1000.50 3000.00\n")
    for pid, (argv, start) in procs.items():
        d = tmp_path / pid
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        # pid (comm) state ppid pgrp session tty_nr tpgid flags，再 12 项，第 22 项是 starttime
        (d / "stat").write_text("%s (rust desk) S 1 %s 0 0 -1 4194304 %s %d 0 0\n" % (
            pid, pid, " ".join(["0"] * 12), start * hz))
    servers = R.rustdesk_servers(str(tmp_path))
    assert [(s["pid"], round(s["age"], 2)) for s in servers] == [(201, 100.5)]
    assert servers[0]["uid"] == R.os.stat(str(tmp_path / "201")).st_uid


# --------------------------------------------------------------------------
# stop-service 标记：服务启动后马上停止
# --------------------------------------------------------------------------

STOPPED_TOML = "[options]\nstop-service = 'Y'\ndirect-server = 'Y'\n"


def test_clear_stop_service_text():
    new, changed = R.clear_stop_service_text(STOPPED_TOML)
    assert changed and new == "[options]\nstop-service = ''\ndirect-server = 'Y'\n"
    assert R.clear_stop_service_text(new) == (new, False)
    new, changed = R.clear_stop_service_text('stop-service="Y"\n')
    assert changed and new == "stop-service=''\n"
    # 别的选项名里带 stop-service 的、值不是 Y 的都不动
    other = "[options]\nno-stop-service = 'Y'\nstop-service = 'N'\n"
    assert R.clear_stop_service_text(other) == (other, False)


def test_clear_stop_service_fixes_every_config_and_keeps_backup(tmp_path, monkeypatch):
    a, b, c = (tmp_path / n / ".config" / "rustdesk" / "RustDesk2.toml" for n in "abc")
    for f, text in ((a, STOPPED_TOML), (b, STOPPED_TOML), (c, "[options]\ndirect-server = 'Y'\n")):
        f.parent.mkdir(parents=True)
        f.write_text(text)
    monkeypatch.setattr(R, "rustdesk_config_files", lambda: [str(a), str(b), str(c)])
    changes = []
    assert R.clear_stop_service(changes) == [str(a), str(b)]
    assert len(changes) == 2 and str(a) in changes[0]
    for f in (a, b):
        assert "stop-service = ''" in f.read_text() and "direct-server = 'Y'" in f.read_text()
        assert (f.parent / "RustDesk2.toml.autorustdesk.bak").read_text() == STOPPED_TOML
    assert c.read_text() == "[options]\ndirect-server = 'Y'\n"
    assert R.clear_stop_service([]) == []


def test_ensure_service_reports_service_that_stops_right_away(rustdesk, monkeypatch):
    m = rustdesk()
    m.service_start()
    m.service_dies = True
    monkeypatch.setattr(R, "rustdesk_service_log",
                        lambda lines=10: "17:50:35 Started RustDesk.\n17:50:35 Stopping RustDesk...")
    monkeypatch.setattr(R, "rustdesk_config_files", lambda: [])
    with pytest.raises(R.ArdError) as e:
        R.ensure_rustdesk_service()
    assert "启动后马上停止" in str(e.value) and "inactive" in str(e.value)
    assert "Stopping RustDesk..." in str(e.value)


def test_configure_distinguishes_stopped_service_from_missing_session(rustdesk, monkeypatch):
    m = rustdesk(server_delay=None)
    m.service_start()
    m.service_dies = True
    monkeypatch.setattr(R, "rustdesk_service_log", lambda lines=10: "")
    monkeypatch.setattr(R, "rustdesk_config_files", lambda: [])
    with pytest.raises(R.ArdError, match="服务没有在运行") as e:
        R.configure_rustdesk(args(), PASSWORD, [])
    assert "没有登录界面" not in str(e.value)


def test_ensure_service_fails_when_systemctl_fails(rustdesk, monkeypatch):
    m = rustdesk()
    real = m.systemctl

    def failing(*a, timeout=60):
        if a[0] == "start":
            return 1, "Failed to start rustdesk.service: Unit is masked."
        return real(*a, timeout=timeout)

    monkeypatch.setattr(R, "systemctl", failing)
    monkeypatch.setattr(R, "rustdesk_service_log", lambda lines=10: "")
    monkeypatch.setattr(R, "rustdesk_config_files", lambda: [])
    with pytest.raises(R.ArdError, match="masked"):
        R.ensure_rustdesk_service()


def test_ensure_service_ok_when_it_keeps_running(rustdesk):
    m = rustdesk()
    R.ensure_rustdesk_service()
    assert m.svc is not None
