#!/usr/bin/env python3
"""测试用的假 rustdesk 可执行文件（在假的电脑 B 上运行，兼容 Python 3.8）。

模拟真实 RustDesk 的命令行行为：
  --version / --get-id / --password / --option / --service / --connect
--service 在 direct-server=Y 时监听 direct-access-port（默认 21118）。
"""

import json
import os
import socket
import sys
import time

STATE = os.environ.get("FAKE_RUSTDESK_STATE", "/var/lib/fake-rustdesk")
VERSION_FILE = "/usr/share/rustdesk/VERSION"


def _path(name):
    return os.path.join(STATE, name)


def _load_options():
    try:
        with open(_path("options.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_options(opts):
    os.makedirs(STATE, exist_ok=True)
    tmp = _path("options.json.tmp")
    with open(tmp, "w") as f:
        json.dump(opts, f)
    os.replace(tmp, _path("options.json"))


def _service_running():
    try:
        with open(_path("service.pid")) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _is_root():
    return os.geteuid() == 0


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
        print("123456789" if _service_running() else "")
        return 0
    if cmd == "--password":
        if not _is_root():
            print("Installation and administrative privileges required!")
            return 0
        if not _service_running():
            print("Failed to connect to the IPC server")
            return 0
        os.makedirs(STATE, exist_ok=True)
        with open(_path("password.txt"), "w") as f:
            f.write(argv[1])
        print("Done!")
        return 0
    if cmd == "--option":
        if not _is_root():
            print("Installation and administrative privileges required!")
            return 0
        opts = _load_options()
        if len(argv) == 2:
            print(opts.get(argv[1], ""))
        elif len(argv) == 3:
            if argv[2]:
                opts[argv[1]] = argv[2]
            else:
                opts.pop(argv[1], None)
            _save_options(opts)
        return 0
    if cmd == "--connect":
        with open("/tmp/fake-rustdesk-connect.log", "a") as f:
            f.write(" ".join(argv) + "\n")
        return 0
    if cmd == "--service":
        return run_service()
    print("unknown args: %s" % " ".join(argv))
    return 0


def run_service():
    os.makedirs(STATE, exist_ok=True)
    with open(_path("service.pid"), "w") as f:
        f.write(str(os.getpid()))
    listener = None
    port = None
    while True:
        opts = _load_options()
        want = opts.get("direct-server") == "Y"
        want_port = int(opts.get("direct-access-port") or 21118)
        if want and (listener is None or port != want_port):
            if listener is not None:
                listener.close()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("0.0.0.0", want_port))
            listener.listen(5)
            listener.settimeout(0.5)
            port = want_port
        elif not want and listener is not None:
            listener.close()
            listener = None
        if listener is not None:
            try:
                conn, _addr = listener.accept()
                conn.sendall(b"FAKE-RUSTDESK\n")
                conn.close()
            except socket.timeout:
                pass
        else:
            time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
