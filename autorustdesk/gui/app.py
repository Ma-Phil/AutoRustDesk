"""启动图形界面。"""

import ctypes
import os
import shutil
import signal
import subprocess
import sys
from typing import Optional

# Qt 6.5 起，xcb（X11）平台插件依赖 libxcb-cursor0，而 Ubuntu 默认没有安装。
# 缺它时 Qt 会直接 abort（"已放弃 (核心已转储)"），所以启动前先检查。
XCB_CURSOR = "libxcb-cursor.so.0"
MISSING_XCB_CURSOR = (
    "缺少系统库 libxcb-cursor0（图形界面库 Qt 6.5 及以上需要它）。请先执行：\n\n"
    "    sudo apt install libxcb-cursor0\n\n"
    "然后重新启动 AutoRustDesk。"
)
NO_DISPLAY = (
    "没有检测到图形桌面（DISPLAY / WAYLAND_DISPLAY 未设置）。\n"
    "请在桌面环境中启动；没有桌面时可以用命令行模式：AutoRustDesk connect --help"
)


def _loadable(name: str) -> bool:
    """能否加载某个共享库（打包版优先看程序包里自带的）。"""
    candidates = [name]
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidates.insert(0, os.path.join(bundled, name))
    for c in candidates:
        try:
            ctypes.CDLL(c)
            return True
        except OSError:
            continue
    return False


def check_qt_platform() -> Optional[str]:
    """检查 Qt 能否在当前桌面启动；不能时返回给用户看的说明。

    Wayland 会话下如果缺 libxcb-cursor0，改用 Qt 的 wayland 插件，不算错误。
    """
    if not sys.platform.startswith("linux"):
        return None
    platform = os.environ.get("QT_QPA_PLATFORM", "")
    if platform and "xcb" not in platform:
        return None
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    if not os.environ.get("DISPLAY") and not wayland:
        return NO_DISPLAY
    if _loadable(XCB_CURSOR):
        return None
    if wayland:
        os.environ["QT_QPA_PLATFORM"] = "wayland"
        return None
    return MISSING_XCB_CURSOR


def show_error_without_qt(message: str) -> None:
    """Qt 起不来时的报错：从应用菜单启动时看不到终端，所以同时尝试桌面弹窗。"""
    print(message, file=sys.stderr)
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    for cmd in (["zenity", "--error", "--no-wrap", "--title=AutoRustDesk", "--text=" + message],
                ["notify-send", "AutoRustDesk", message]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
                return
            except (OSError, subprocess.SubprocessError):
                continue


def run() -> int:
    problem = check_qt_platform()
    if problem:
        show_error_without_qt(problem)
        return 1
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as e:
        show_error_without_qt(
            "无法加载图形界面库 PySide6：%s\n\n"
            "源码运行时请执行：pip install -r requirements.txt；"
            "打包版请确认是在装好依赖的电脑上打的包。" % e)
        return 1
    from .. import APP_NAME
    from .main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    # Ctrl+C 能退出
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    win = MainWindow()
    win.show()
    return app.exec()
