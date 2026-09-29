"""入口：

    python -m autorustdesk               启动图形界面
    python -m autorustdesk bundle ...    制作/查看离线部署包
    python -m autorustdesk connect ...   命令行模式执行完整流程
    python -m autorustdesk helper        （内部）以管理员权限运行的网络助手
    python -m autorustdesk selftest      （内部）检查打包是否完整，输出本机网卡等信息
"""

import argparse
import json
import os
import sys
import warnings

# Ubuntu 20.04 的 Python 是 3.8，cryptography（paramiko 依赖）每次启动都会提示
# "Python 3.8 is no longer supported"。只是提醒，不影响使用，不显示给用户。
warnings.filterwarnings("ignore", message=r".*Python 3\.8 is no longer supported.*")


def selftest() -> int:
    """打包后自检：各模块都能加载、系统接口能调用。"""
    from . import __version__

    info = {"version": __version__, "platform": sys.platform, "python": sys.version.split()[0],
            "frozen": bool(getattr(sys, "frozen", False))}
    from .core import nic

    info["nics"] = [n.label for n in nic.list_nics(include_virtual=True)]
    from .helper.packetio import libpcap_error, libpcap_version

    info["libpcap"] = libpcap_version() or "不可用（%s）" % libpcap_error()
    import paramiko

    info["paramiko"] = paramiko.__version__
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from .gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([sys.argv[0]])
    win = MainWindow()
    info["gui"] = win.windowTitle()
    win.close()
    app.processEvents()
    text = json.dumps(info, ensure_ascii=False, indent=2)
    if sys.stdout is not None:
        print(text)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["helper"]:
        from .helper.server import main as helper_main

        return helper_main(argv[1:])
    if argv[:1] == ["selftest"]:
        return selftest()

    parser = argparse.ArgumentParser(prog="autorustdesk", description="AutoRustDesk 网线直连远控工具")
    sub = parser.add_subparsers(dest="cmd")
    from .bundle.cli import add_subparser as add_bundle

    add_bundle(sub)
    from .cli import add_subparser as add_connect

    add_connect(sub)
    g = sub.add_parser("gui", help="启动图形界面（默认）")
    g.set_defaults(func=None)

    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        from .gui.app import run as run_gui

        return run_gui()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
