"""供 pkexec 直接执行：pkexec /usr/bin/python3 /路径/autorustdesk/helper/__main__.py

pkexec 会清空环境变量，所以这里自己把包所在目录加进 sys.path。
"""

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from autorustdesk.helper.server import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
