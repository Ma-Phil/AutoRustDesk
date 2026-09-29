"""入口：

    python -m autorustdesk               启动图形界面
    python -m autorustdesk bundle ...    制作/查看离线部署包
    python -m autorustdesk connect ...   命令行模式执行完整流程
    python -m autorustdesk helper        （内部）以 root 运行的网络助手
"""

import argparse
import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["helper"]:
        from .helper.server import main as helper_main

        return helper_main(argv[1:])

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
