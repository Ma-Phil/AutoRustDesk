"""启动图形界面。"""

import signal
import sys


def run() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("缺少 PySide6，请先执行：pip install -r requirements.txt", file=sys.stderr)
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
