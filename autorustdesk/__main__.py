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
    from .bundle import Bundle
    from .core.builtin import builtin_bundle, builtin_client

    bundled = builtin_bundle()
    info["builtin_bundle"] = Bundle.open(bundled).summary() if bundled else ""
    client = builtin_client()
    info["builtin_client"] = os.path.basename(client[0]) if client else ""
    # 子进程（nmcli、RustDesk 等）不能继承自带库的目录，见 _restore_child_env()
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass and meipass in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep):
        raise SystemExit("自检失败：子进程会继承 LD_LIBRARY_PATH=%s" % os.environ["LD_LIBRARY_PATH"])
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


def _utf8_output() -> None:
    """Windows 上输出被重定向到文件、管道或 NUL 时默认用系统代码页（如 cp1252），中文会报错；改用 UTF-8。

    真正的控制台窗口 Python 本来就用 UTF-8 输出，不受影响。
    （不能用 isatty() 判断：重定向到 NUL 时 isatty() 也是 True。）
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _restore_child_env() -> None:
    """Linux 打包版：PyInstaller 启动时把程序自带库的目录加到了 LD_LIBRARY_PATH 最前面，
    所有子进程都会继承。nmcli、ip、RustDesk 这些系统程序就会加载到打包机（Ubuntu 20.04）的旧版
    glib、libstdc++ 等库，在更新的系统上无法运行。

    本进程要用的库在启动时已经定位好了（动态链接器只在进程启动时读这个变量），
    这里把它恢复成用户原来的值，只影响之后启动的子进程。
    """
    if not getattr(sys, "frozen", False) or sys.platform in ("win32", "darwin"):
        return
    orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig is not None:
        os.environ["LD_LIBRARY_PATH"] = orig
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _restore_child_env()
    _utf8_output()
    if argv[:1] == ["helper"]:
        from .helper.server import main as helper_main

        return helper_main(argv[1:])
    if argv[:1] == ["selftest"]:
        return selftest()

    parser = argparse.ArgumentParser(prog="autorustdesk", description="AutoRustDesk 网线直连远控工具")
    sub = parser.add_subparsers(dest="cmd")
    from .bundle.cli import add_subparser as add_bundle

    add_bundle(sub)
    if argv[:1] != ["bundle"]:
        # 制作离线包只需要标准库（和 zstandard），不加载 SSH、界面等依赖
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
