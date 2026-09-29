"""命令行模式：不开图形界面，直接执行完整流程。

    python -m autorustdesk connect --bundle 离线包.tar --user robot
"""

import argparse
import getpass
import sys
import time
from typing import List, Optional

from .core.devices import DeviceRegistry
from .core.settings import Settings
from .core.workflow import STEP_TITLES, Credentials, Ui, Workflow, WorkflowError

STATE_MARK = {"running": "…", "done": "✔", "skipped": "↷", "failed": "✘", "warning": "!"}


class ConsoleUi(Ui):
    def __init__(self, assume_yes: bool = False, verbose: bool = False, password: str = ""):
        self.assume_yes = assume_yes
        self.verbose = verbose
        self.password = password
        self._last_progress = -1

    def log(self, msg: str, level: str = "info") -> None:
        if level == "debug" and not self.verbose:
            return
        prefix = {"warning": "[注意] ", "error": "[错误] "}.get(level, "")
        print("%s %s%s" % (time.strftime("%H:%M:%S"), prefix, msg), flush=True)

    def step(self, step_id: str, state: str, detail: str = "") -> None:
        if state == "pending":
            return
        mark = STATE_MARK.get(state, " ")
        print("[%s] %s%s" % (mark, STEP_TITLES.get(step_id, step_id), "：" + detail if detail else ""),
              flush=True)

    def progress(self, text: str, fraction: float) -> None:
        if fraction < 0:
            self._last_progress = -1
            return
        pct = int(fraction * 100)
        if pct // 10 != self._last_progress // 10:
            self._last_progress = pct
            print("    %s %d%%" % (text, pct), flush=True)

    def ask_credentials(self, title: str, username: str, error: str = "") -> Optional[Credentials]:
        if error:
            print("[错误] " + error)
            if not sys.stdin.isatty():
                return None
        if self.password and not error and username:
            return Credentials(username, self.password)
        if not sys.stdin.isatty():
            return None
        print(title)
        user = input("用户名 [%s]: " % username).strip() or username
        pw = getpass.getpass("密码: ")
        return Credentials(user, pw)

    def confirm(self, title: str, text: str, default: bool = True) -> bool:
        if self.assume_yes:
            return True
        if not sys.stdin.isatty():
            return default
        ans = input("%s\n%s [%s]: " % (title, text, "Y/n" if default else "y/N")).strip().lower()
        if not ans:
            return default
        return ans in ("y", "yes", "是")

    def choose(self, title: str, options: List[str]) -> Optional[int]:
        print(title)
        for i, o in enumerate(options, 1):
            print("  %d) %s" % (i, o))
        if not sys.stdin.isatty():
            return 0
        ans = input("选择 [1]: ").strip() or "1"
        try:
            return int(ans) - 1
        except ValueError:
            return None


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("connect", help="命令行模式：执行完整流程并连接电脑 B")
    p.add_argument("--iface", help="直连网卡（默认自动选择插了网线的）")
    p.add_argument("--bundle", help="离线部署包")
    p.add_argument("--user", help="B 的 SSH 用户名")
    p.add_argument("--password", help="B 的 SSH 密码（不填则交互输入）")
    p.add_argument("--subnet", help="直连网段，默认 192.168.77.0/24")
    p.add_argument("--quick", action="store_true", help="已配置过的设备直接连接")
    p.add_argument("--no-launch", action="store_true", help="不自动打开本机 RustDesk")
    p.add_argument("--keep-network", action="store_true",
                   help="完成后保持直连网络（按回车后再恢复）")
    p.add_argument("-y", "--yes", action="store_true", help="所有确认都回答「是」")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=run_cli)


def run_cli(args: argparse.Namespace) -> int:
    settings = Settings.load()
    if args.iface:
        settings.iface = args.iface
        settings.show_virtual_nics = True
    if args.bundle:
        settings.bundle_path = args.bundle
    if args.user:
        settings.ssh_user = args.user
    if args.subnet:
        settings.subnet = args.subnet
    if args.no_launch:
        settings.auto_launch = False
    ui = ConsoleUi(assume_yes=args.yes, verbose=args.verbose, password=args.password or "")
    wf = Workflow(settings, DeviceRegistry(), ui)
    if args.password:
        wf.session_ssh_password = args.password
    try:
        result = wf.run("quick" if args.quick else "full")
    except WorkflowError as e:
        print("[错误] %s" % e)
        wf.restore_network()
        return 1
    try:
        if result.get("ok"):
            print("\n完成：地址 %s，RustDesk 密码 %s" % (result["ip"], result["password"]))
            if args.keep_network and sys.stdin.isatty():
                input("按回车恢复本机网络并退出……")
        return 0 if result.get("ok") else 1
    finally:
        wf.restore_network()
