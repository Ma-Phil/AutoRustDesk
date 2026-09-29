"""本地配置与数据目录。"""

import os
import sys


def config_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "AutoRustDesk")
    elif sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/AutoRustDesk")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        d = os.path.join(base, "autorustdesk")
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def log_dir() -> str:
    d = os.path.join(config_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def write_private(path: str, text: str) -> None:
    """原子写入，只允许当前用户读写。"""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
