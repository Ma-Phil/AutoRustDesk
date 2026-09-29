"""电脑 A 上的 RustDesk 客户端：查找并以"IP + 密码"发起连接。"""

import os
import shutil
import subprocess
import sys
from typing import List, Optional

from .builtin import builtin_client

LINUX_CANDIDATES = ["/usr/bin/rustdesk", "/usr/share/rustdesk/rustdesk", "/usr/local/bin/rustdesk"]
FLATPAK_ID = "com.rustdesk.RustDesk"


def windows_candidates() -> List[str]:
    dirs = [os.environ.get("ProgramFiles") or r"C:\Program Files",
            os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"]
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(os.path.join(os.environ["LOCALAPPDATA"], "Programs"))
    return [os.path.join(d, "RustDesk", "rustdesk.exe") for d in dirs]


def mac_candidates() -> List[str]:
    apps = ["/Applications", os.path.expanduser("~/Applications")]
    return [os.path.join(a, "RustDesk.app", "Contents", "MacOS", "RustDesk") for a in apps]


def find_client(custom: str = "") -> Optional[List[str]]:
    """返回启动 RustDesk 的命令前缀，找不到返回 None。"""
    if custom:
        if custom.endswith(".app") and os.path.isdir(custom):
            # macOS：选的是 RustDesk.app，找到里面的可执行文件
            inner = os.path.join(custom, "Contents", "MacOS", "RustDesk")
            return [inner] if os.path.isfile(inner) else None
        if os.path.isfile(custom) and os.access(custom, os.X_OK):
            if custom.lower().endswith(".appimage"):
                return [custom, "--appimage-extract-and-run"]
            return [custom]
        return None
    if sys.platform == "win32":
        cands = windows_candidates()
    elif sys.platform == "darwin":
        cands = mac_candidates()
    else:
        cands = LINUX_CANDIDATES
    which = shutil.which("rustdesk")
    if which:
        return [which]
    for c in cands:
        if os.path.isfile(c):
            return [c]
    if sys.platform.startswith("linux") and shutil.which("flatpak"):
        rc = subprocess.run(["flatpak", "info", FLATPAK_ID], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode
        if rc == 0:
            return ["flatpak", "run", FLATPAK_ID]
    # 最后用程序自带的 RustDesk（完整版安装包里有）
    return builtin_client()


def connect_command(prefix: List[str], target: str, password: str) -> List[str]:
    return list(prefix) + ["--connect", target, "--password", password]


def launch(prefix: List[str], target: str, password: str) -> subprocess.Popen:
    """启动 RustDesk 连接窗口（不等待其退出）。"""
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(connect_command(prefix, target, password), **kwargs)
