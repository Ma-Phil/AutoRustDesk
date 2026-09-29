"""在电脑 B 上执行的脚本（通过 SSH 上传）。"""

import os

REMOTE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ard_remote.py")
