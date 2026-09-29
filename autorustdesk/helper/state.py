"""助手对本机网卡所做修改的记录。

助手异常退出（断电、被强制结束）后，下次启动时据此把网卡恢复原状。
记录文件普通用户也能读，界面据此提示"有上次遗留的网络配置"。
"""

import json
import os
import sys
from typing import Dict, Optional


def state_dir() -> str:
    if sys.platform == "win32":
        return os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", "AutoRustDesk")
    if sys.platform == "darwin":
        return "/var/run/autorustdesk"
    return "/run/autorustdesk"


STATE_DIR = state_dir()
STATE_FILE = os.path.join(STATE_DIR, "links.json")


def load_state(path: Optional[str] = None) -> Dict:
    try:
        with open(path or STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: Dict, path: Optional[str] = None) -> None:
    path = path or STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)
