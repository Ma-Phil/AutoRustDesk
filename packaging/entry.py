"""PyInstaller 入口（打包后的程序也用它以 `AutoRustDesk helper` 方式启动网络助手）。"""

import sys

from autorustdesk.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
