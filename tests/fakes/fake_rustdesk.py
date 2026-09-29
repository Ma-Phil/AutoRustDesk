#!/usr/bin/env python3
"""测试用的假 rustdesk 可执行文件（在假的电脑 B 上运行，兼容 Python 3.8）。

按 RustDesk 1.4 的进程结构模拟命令行行为：
  --version / --get-id / --password / --option / --service / --server / --connect
--service（root）启动 1.5 秒后启动 --server 子进程（真实的 RustDesk 在有图形会话时，以会话
用户的身份启动它）；直连端口由 --server 在 direct-server=Y 时监听 direct-access-port（默认 21118）。

配置有三份，和真实的 RustDesk 一样会互相覆盖（都在 $FAKE_RUSTDESK_STATE 下）：
  options.json、password.txt  root 的配置文件
  service.json                --service 内存里的配置：启动时读一次配置文件，之后不再读
  server.json                 --server 内存里的配置
--server 启动 0.25 秒后开始监听 IPC（server.sock），0.35 秒时从 --service 同步一次配置
（覆盖自己的），之后自己的配置一变就推给 --service，--service 再写回配置文件。
--option/--password 连得上 IPC 就改 --server 的配置；--option 还总会写一份配置文件，
所以连不上 IPC 时只写了配置文件；--password 连不上 IPC 就失败。
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

STATE = os.environ.get("FAKE_RUSTDESK_STATE", "/var/lib/fake-rustdesk")
VERSION_FILE = "/usr/share/rustdesk/VERSION"
SERVER_DELAY = 1.5
IPC_UP = 0.25
SYNC = 0.35


def _empty():
    return {"options": {}, "password": ""}


def _path(name):
    return os.path.join(STATE, name)


def _write(name, text):
    os.makedirs(STATE, exist_ok=True)
    tmp = _path("%s.%d.tmp" % (name, os.getpid()))
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, _path(name))


def _load(name):
    try:
        with open(_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return _empty()


def _save(name, value):
    _write(name, json.dumps(value, sort_keys=True))


def _root_config():
    try:
        with open(_path("options.json")) as f:
            options = json.load(f)
    except (OSError, ValueError):
        options = {}
    try:
        with open(_path("password.txt")) as f:
            password = f.read().strip()
    except OSError:
        password = ""
    return {"options": options, "password": password}


def _save_root_config(cfg):
    _write("options.json", json.dumps(cfg["options"], sort_keys=True))
    _write("password.txt", cfg["password"])


def _pid_alive(name):
    try:
        with open(_path(name)) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return 0


def _ipc_up():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(_path("server.sock"))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _is_root():
    return os.geteuid() == 0 or os.environ.get("FAKE_RUSTDESK_ASSUME_ROOT") == "1"


def main(argv):
    if not argv:
        print("fake rustdesk gui")
        return 0
    cmd = argv[0]
    if cmd == "--version":
        try:
            with open(VERSION_FILE) as f:
                print(f.read().strip())
        except OSError:
            print("1.4.2")
        return 0
    if cmd == "--get-id":
        # 连不上 IPC 时真实的 RustDesk 读配置文件里的 ID，照样能打印出来
        print("123456789")
        return 0
    if cmd == "--password":
        if not _is_root():
            print("Installation and administrative privileges required!")
            return 0
        if not _ipc_up():
            print("No such file or directory (os error 2)")
            return 0
        server = _load("server.json")
        server["password"] = argv[1]
        _save("server.json", server)
        root = _root_config()
        root["password"] = argv[1]
        _save_root_config(root)
        print("Done!")
        return 0
    if cmd == "--option":
        if not _is_root():
            print("Installation and administrative privileges required!")
            return 0
        up = _ipc_up()
        opts = dict((_load("server.json") if up else _root_config())["options"])
        if len(argv) == 2:
            print(opts.get(argv[1], ""))
        elif len(argv) == 3:
            if argv[2]:
                opts[argv[1]] = argv[2]
            else:
                opts.pop(argv[1], None)
            if up:
                server = _load("server.json")
                server["options"] = opts
                _save("server.json", server)
            root = _root_config()
            root["options"] = opts
            _save_root_config(root)
        return 0
    if cmd == "--connect":
        with open("/tmp/fake-rustdesk-connect.log", "a") as f:
            f.write(" ".join(argv) + "\n")
        return 0
    if cmd == "--service":
        return run_service()
    if cmd == "--server":
        return run_server()
    print("unknown args: %s" % " ".join(argv))
    return 0


def run_service():
    old = _pid_alive("server.pid")
    if old:
        os.kill(old, signal.SIGKILL)  # 和真实的一样，先清掉上次留下的 --server
    _write("service.pid", str(os.getpid()))
    _save("service.json", _root_config())
    child = []

    def stop(*_args):
        for p in child:
            p.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    time.sleep(SERVER_DELAY)
    while True:
        if not child or child[0].poll() is not None:
            child[:] = [subprocess.Popen([os.path.realpath(__file__), "--server"])]
        time.sleep(0.5)


def run_server():
    _write("server.pid", str(os.getpid()))
    _save("server.json", _empty())
    time.sleep(IPC_UP)
    ipc = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.remove(_path("server.sock"))
    except OSError:
        pass
    ipc.bind(_path("server.sock"))
    ipc.listen(16)

    def serve_ipc():
        while True:
            conn, _ = ipc.accept()
            conn.close()

    threading.Thread(target=serve_ipc, daemon=True).start()
    time.sleep(SYNC - IPC_UP)
    last = _load("service.json")
    _save("server.json", last)
    listener = None
    port = None
    busy = None
    while True:
        cur = _load("server.json")
        if cur != last:
            _save("service.json", cur)
            _save_root_config(cur)
            last = cur
        opts = cur["options"]
        want = opts.get("direct-server") == "Y"
        want_port = int(opts.get("direct-access-port") or 21118)
        if want and want_port != port and want_port != busy:
            if listener is not None:
                listener.close()
                listener = None
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", want_port))
                sock.listen(5)
            except OSError:
                # 和真实的一样：端口被占用时不退出，等端口设置变了再试
                sock.close()
                port, busy = None, want_port
            else:
                sock.settimeout(0.1)
                listener, port, busy = sock, want_port, None
        elif not want and listener is not None:
            listener.close()
            listener, port = None, None
        if listener is not None:
            try:
                conn, _addr = listener.accept()
                conn.sendall(b"FAKE-RUSTDESK\n")
                conn.close()
            except socket.timeout:
                pass
        else:
            time.sleep(0.1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
