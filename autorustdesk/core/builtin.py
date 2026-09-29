"""随程序一起打包的资源，让电脑 A 全程不用联网：

- bundle/：电脑 B 的离线部署包（RustDesk + Ubuntu 各版本的依赖）
- rustdesk/：电脑 A 用的 RustDesk 客户端（Windows 为 exe，Linux 为 AppImage；
  macOS 的 RustDesk.app 放在安装盘里，和本程序一起拖进"应用程序"）

打包版在程序目录（PyInstaller 的 sys._MEIPASS，或可执行文件所在目录）下查找；
源码运行时可以用环境变量 AUTORUSTDESK_RESOURCES 指定目录。
"""

import glob
import os
import sys
from typing import List, Optional


def resource_dirs() -> List[str]:
    dirs: List[str] = []
    override = os.environ.get("AUTORUSTDESK_RESOURCES")
    if override:
        dirs.append(override)
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(meipass)
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    seen = set()
    return [d for d in dirs if not (d in seen or seen.add(d))]


def _find(sub: str, patterns: List[str]) -> List[str]:
    found: List[str] = []
    for d in resource_dirs():
        for pat in patterns:
            found.extend(sorted(glob.glob(os.path.join(d, sub, pat))))
    return found


def builtin_bundle() -> str:
    """内置的离线包路径，没有时返回空字符串（有多个时取文件名排序最后的）。"""
    found = _find("bundle", ["*.tar"])
    return found[-1] if found else ""


def builtin_client() -> Optional[List[str]]:
    """内置的 RustDesk 客户端的启动命令前缀，没有时返回 None。"""
    if sys.platform == "win32":
        found = _find("rustdesk", ["*.exe"])
        return [found[-1]] if found else None
    if sys.platform == "darwin":
        # 直接从安装盘里运行本程序时，RustDesk.app 就在旁边
        app = os.path.abspath(sys.executable)
        for _ in range(3):  # AutoRustDesk.app/Contents/MacOS/AutoRustDesk
            app = os.path.dirname(app)
        exe = os.path.join(os.path.dirname(app), "RustDesk.app", "Contents", "MacOS", "RustDesk")
        return [exe] if getattr(sys, "frozen", False) and os.path.isfile(exe) else None
    found = _find("rustdesk", ["*.AppImage", "*.appimage"])
    if not found:
        return None
    # 解压后运行，不依赖 FUSE（新系统不一定装了 libfuse2）
    return [found[-1], "--appimage-extract-and-run"]
