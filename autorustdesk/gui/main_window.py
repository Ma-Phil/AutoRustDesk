"""主窗口。"""

import html
import os
import time
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QFont, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..bundle import Bundle, BundleError
from ..core import nic as nicmod
from ..core import rustdesk_local
from ..core.devices import DeviceRegistry
from ..core.paths import log_dir
from ..core.settings import Settings
from ..core.workflow import FAILED, STEPS, Workflow, stale_network_config
from .bridge import GuiUi, Request, WorkflowRunner
from .dialogs import BundleDialog, CredentialsDialog, DevicesDialog, SettingsDialog

STATE_STYLE = {
    "pending": ("○", "#9e9e9e"),
    "running": ("◐", "#1565c0"),
    "done": ("✔", "#2e7d32"),
    "skipped": ("↷", "#757575"),
    "failed": ("✘", "#c62828"),
    "warning": ("!", "#ef6c00"),
}
LEVEL_COLOR = {"warning": "#ef6c00", "error": "#c62828", "debug": "#8a8a8a"}


class StepRow(QWidget):
    def __init__(self, index: int, title: str):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        self.icon = QLabel()
        self.icon.setFixedWidth(22)
        self.icon.setAlignment(Qt.AlignCenter)
        f = self.icon.font()
        f.setPointSize(f.pointSize() + 2)
        self.icon.setFont(f)
        self.title = QLabel("%d. %s" % (index, title))
        self.title.setMinimumWidth(150)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setStyleSheet("color: #616161;")
        lay.addWidget(self.icon)
        lay.addWidget(self.title)
        lay.addWidget(self.detail, 1)
        self.set_state("pending", "")

    def set_state(self, state: str, detail: str) -> None:
        sym, color = STATE_STYLE.get(state, ("?", "#000"))
        self.icon.setText(sym)
        self.icon.setStyleSheet("color: %s; font-weight: bold;" % color)
        self.title.setStyleSheet("font-weight: bold;" if state == "running" else "")
        self.detail.setText(detail)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = Settings.load()
        self.registry = DeviceRegistry()
        self.ui = GuiUi()
        self.workflow = Workflow(self.settings, self.registry, self.ui)
        self.runner: Optional[WorkflowRunner] = None
        self.device_info: Dict = {}
        self.log_lines: List[str] = []
        self.closing = False

        self.setWindowTitle("%s %s — 网线直连远程控制" % (APP_NAME, __version__))
        self.resize(1080, 760)
        self._build_menu()
        self._build_ui()
        self._connect_signals()
        self.refresh_nics()
        self._load_bundle(self.settings.bundle_path, quiet=True)
        self.link_timer = QTimer(self)
        self.link_timer.timeout.connect(self._update_link)
        self.link_timer.start(1000)
        self.log("欢迎使用 %s。用网线连接电脑 B 并确认 B 已开机，然后点击「一键连接」。" % APP_NAME)
        stale = stale_network_config()
        if stale:
            self.log("检测到上次异常退出遗留的网络配置（%s），请点击「恢复本机网络」清理。" % "、".join(stale),
                     "warning")

    # ------------------------------------------------------------------ 界面

    def _build_menu(self) -> None:
        bar = self.menuBar()
        m = bar.addMenu("文件")
        a = QAction("选择离线包…", self)
        a.triggered.connect(self.choose_bundle)
        m.addAction(a)
        a = QAction("制作离线包…", self)
        a.triggered.connect(self.make_bundle)
        m.addAction(a)
        m.addSeparator()
        a = QAction("导出日志…", self)
        a.triggered.connect(self.export_log)
        m.addAction(a)
        m.addSeparator()
        a = QAction("退出", self)
        a.triggered.connect(self.close)
        m.addAction(a)
        m = bar.addMenu("工具")
        a = QAction("设置…", self)
        a.triggered.connect(self.open_settings)
        m.addAction(a)
        a = QAction("已知设备…", self)
        a.triggered.connect(self.open_devices)
        m.addAction(a)
        a = QAction("恢复本机网络", self)
        a.triggered.connect(self.restore_network)
        m.addAction(a)
        m = bar.addMenu("帮助")
        a = QAction("关于", self)
        a.triggered.connect(self.about)
        m.addAction(a)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        self.setCentralWidget(central)

        # 顶部：网卡与离线包
        top = QGroupBox("连接准备")
        grid = QGridLayout(top)
        self.nic_combo = QComboBox()
        self.nic_combo.setMinimumWidth(420)
        self.nic_combo.currentIndexChanged.connect(self._nic_changed)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_nics)
        self.link_label = QLabel("")
        grid.addWidget(QLabel("直连网卡"), 0, 0)
        grid.addWidget(self.nic_combo, 0, 1)
        grid.addWidget(refresh, 0, 2)
        grid.addWidget(self.link_label, 0, 3)
        self.bundle_edit = QLineEdit()
        self.bundle_edit.setReadOnly(True)
        self.bundle_edit.setPlaceholderText("B 上没有 RustDesk 时需要：选择离线部署包（.tar）")
        choose = QPushButton("选择…")
        choose.clicked.connect(self.choose_bundle)
        make = QPushButton("制作离线包…")
        make.clicked.connect(self.make_bundle)
        grid.addWidget(QLabel("离线包"), 1, 0)
        grid.addWidget(self.bundle_edit, 1, 1)
        grid.addWidget(choose, 1, 2)
        grid.addWidget(make, 1, 3)
        self.bundle_label = QLabel("")
        self.bundle_label.setStyleSheet("color: #616161;")
        grid.addWidget(self.bundle_label, 2, 1, 1, 3)
        grid.setColumnStretch(1, 1)
        root.addWidget(top)

        # 中部：步骤 + 设备
        split = QSplitter(Qt.Horizontal)
        steps_box = QGroupBox("进度")
        sl = QVBoxLayout(steps_box)
        self.step_rows: Dict[str, StepRow] = {}
        for i, (sid, title) in enumerate(STEPS, 1):
            row = StepRow(i, title)
            self.step_rows[sid] = row
            sl.addWidget(row)
        sl.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        sl.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.full_btn = QPushButton("一键连接")
        self.full_btn.setMinimumHeight(40)
        self.full_btn.setStyleSheet(
            "QPushButton { background: #1565c0; color: white; font-weight: bold; padding: 6px 18px;"
            " border-radius: 4px; } QPushButton:disabled { background: #90a4ae; }")
        self.full_btn.setToolTip("完整流程：发现 B、按需安装并配置 RustDesk，然后连接")
        self.full_btn.clicked.connect(lambda: self.start("full"))
        self.quick_btn = QPushButton("快速连接")
        self.quick_btn.setMinimumHeight(40)
        self.quick_btn.setToolTip("已经配置过的设备：直接连接，跳过安装和配置")
        self.quick_btn.clicked.connect(lambda: self.start("quick"))
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setMinimumHeight(40)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.restore_btn = QPushButton("恢复本机网络")
        self.restore_btn.setMinimumHeight(40)
        self.restore_btn.setToolTip("停止 DHCP 服务，把网卡恢复成原来的设置")
        self.restore_btn.clicked.connect(self.restore_network)
        for b in (self.full_btn, self.quick_btn, self.stop_btn, self.restore_btn):
            buttons.addWidget(b)
        sl.addLayout(buttons)
        split.addWidget(steps_box)

        dev_box = QGroupBox("电脑 B")
        dl = QGridLayout(dev_box)
        self.dev_fields: Dict[str, QLabel] = {}
        rows = [("name", "设备"), ("mac", "MAC"), ("link_ip", "RustDesk 地址"),
                ("password", "RustDesk 密码"), ("rustdesk_version", "RustDesk 版本"),
                ("system", "系统"), ("display", "显示"), ("configured_at", "配置时间")]
        for r, (key, label) in enumerate(rows):
            dl.addWidget(QLabel(label), r, 0, Qt.AlignTop)
            val = QLabel("—")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.dev_fields[key] = val
            dl.addWidget(val, r, 1)
        self.show_pw = QCheckBox("显示密码")
        self.show_pw.toggled.connect(lambda _: self._render_device())
        dl.addWidget(self.show_pw, len(rows), 1)
        btns = QHBoxLayout()
        self.copy_ip = QPushButton("复制地址")
        self.copy_ip.clicked.connect(lambda: self._copy("link_ip"))
        self.copy_pw = QPushButton("复制密码")
        self.copy_pw.clicked.connect(lambda: self._copy("password"))
        self.open_rd = QPushButton("打开 RustDesk")
        self.open_rd.clicked.connect(self.open_rustdesk)
        for b in (self.copy_ip, self.copy_pw, self.open_rd):
            btns.addWidget(b)
        dl.addLayout(btns, len(rows) + 1, 0, 1, 2)
        dl.setRowStretch(len(rows) + 2, 1)
        dl.setColumnStretch(1, 1)
        split.addWidget(dev_box)
        split.setSizes([640, 400])

        # 底部：日志
        log_box = QGroupBox("日志")
        ll = QVBoxLayout(log_box)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.TypeWriter)
        self.log_view.setFont(mono)
        ll.addWidget(self.log_view)
        opts = QHBoxLayout()
        self.verbose = QCheckBox("显示详细日志")
        opts.addWidget(self.verbose)
        opts.addStretch(1)
        export = QPushButton("导出日志…")
        export.clicked.connect(self.export_log)
        opts.addWidget(export)
        ll.addLayout(opts)

        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(split)
        vsplit.addWidget(log_box)
        vsplit.setSizes([420, 260])
        root.addWidget(vsplit, 1)
        self._render_device()
        self._set_running(False)

    def _connect_signals(self) -> None:
        self.ui.log_signal.connect(self.log)
        self.ui.step_signal.connect(self._on_step)
        self.ui.device_signal.connect(self._on_device)
        self.ui.progress_signal.connect(self._on_progress)
        self.ui.request_signal.connect(self._on_request)

    # ------------------------------------------------------------------ 网卡

    def refresh_nics(self) -> None:
        current = self.nic_combo.currentData() or self.settings.iface
        self.nic_combo.blockSignals(True)
        self.nic_combo.clear()
        nics = nicmod.list_nics(include_virtual=self.settings.show_virtual_nics)
        for n in nics:
            self.nic_combo.addItem(n.label, n.name)
        pick = nicmod.pick_default(nics, current)
        if pick:
            self.nic_combo.setCurrentIndex(max(0, self.nic_combo.findData(pick.name)))
        self.nic_combo.blockSignals(False)
        if not nics:
            self.nic_combo.addItem("没有找到有线网卡（可接 USB 网卡）", "")
        self._nic_changed()

    def _nic_changed(self) -> None:
        self.settings.iface = self.nic_combo.currentData() or ""
        self._update_link()

    def _update_link(self) -> None:
        iface = self.nic_combo.currentData()
        if not iface:
            self.link_label.setText("")
            return
        c = nicmod.carrier(iface)
        if c:
            self.link_label.setText("<span style='color:#2e7d32'>● 已插网线</span>")
        elif c is False:
            self.link_label.setText("<span style='color:#9e9e9e'>○ 未插网线</span>")
        else:
            self.link_label.setText("<span style='color:#9e9e9e'>○ 网卡未启用</span>")

    # ------------------------------------------------------------------ 离线包

    def _load_bundle(self, path: str, quiet: bool = False) -> bool:
        if not path:
            self.bundle_edit.setText("")
            self.bundle_label.setText("B 上已经装有 RustDesk 时可以不选。")
            return False
        try:
            b = Bundle.open(path)
        except BundleError as e:
            if not quiet:
                QMessageBox.warning(self, "离线包", str(e))
            self.bundle_label.setText("<span style='color:#c62828'>%s</span>" % html.escape(str(e)))
            return False
        self.settings.bundle_path = path
        self.bundle_edit.setText(path)
        self.bundle_label.setText(b.summary())
        return True

    def choose_bundle(self) -> None:
        start = os.path.dirname(self.settings.bundle_path) if self.settings.bundle_path else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "选择离线部署包", start, "离线包 (*.tar);;所有文件 (*)")
        if path and self._load_bundle(path):
            self.settings.save()

    def make_bundle(self) -> None:
        dlg = BundleDialog(self, self.settings)
        if dlg.exec() and dlg.result_path:
            self._load_bundle(dlg.result_path)
        self.settings.save()

    # ------------------------------------------------------------------ 运行

    def _set_running(self, running: bool) -> None:
        self.full_btn.setEnabled(not running)
        self.quick_btn.setEnabled(not running)
        self.restore_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.nic_combo.setEnabled(not running)

    def start(self, mode: str) -> None:
        if self.runner and self.runner.isRunning():
            return
        if not self.settings.iface:
            QMessageBox.warning(self, APP_NAME, "请先选择连接电脑 B 的有线网卡")
            return
        self.settings.save()
        self.ui.reset()
        self.progress.setVisible(False)
        self._set_running(True)
        self.log("开始%s……" % ("一键连接" if mode == "full" else "快速连接"))
        self.runner = WorkflowRunner(self.workflow, mode)
        self.runner.finished_with.connect(self._on_finished)
        self.runner.start()

    def stop(self) -> None:
        self.ui.cancel()
        self.stop_btn.setEnabled(False)
        self.log("正在停止……", "warning")

    def restore_network(self) -> None:
        if self.runner and self.runner.isRunning():
            return
        self._set_running(True)
        self.stop_btn.setEnabled(False)
        self.runner = WorkflowRunner(self.workflow, "", action="restore")
        self.runner.finished_with.connect(self._on_restored)
        self.runner.start()

    def _on_restored(self, result: Dict) -> None:
        if self.runner:
            self.runner.wait(5000)
        self._set_running(False)
        self.log("本机网络已恢复")
        if self.closing:
            self.close()

    def _on_finished(self, result: Dict) -> None:
        if self.runner:
            self.runner.wait(5000)
        self._set_running(False)
        self.progress.setVisible(False)
        if result.get("ok"):
            self.log("完成：RustDesk 地址 %s" % result.get("ip"))
            self.statusBar().showMessage("已连接电脑 B：%s" % result.get("ip"))
        elif result.get("cancelled"):
            self.statusBar().showMessage("已取消")
        else:
            self.statusBar().showMessage("失败：%s" % result.get("error", ""))
            if "step" not in result:
                self.log("出错：%s" % result.get("error"), "error")
        if self.closing:
            self.restore_network()

    # ------------------------------------------------------------------ 流程回调

    def _on_step(self, step_id: str, state: str, detail: str) -> None:
        row = self.step_rows.get(step_id)
        if row:
            row.set_state(state, detail)
        if state == FAILED and detail:
            self.statusBar().showMessage(detail)

    def _on_progress(self, text: str, frac: float) -> None:
        if frac < 0:
            self.progress.setVisible(False)
            return
        self.progress.setVisible(True)
        self.progress.setFormat("%s %%p%%" % text)
        self.progress.setValue(int(frac * 100))

    def _on_device(self, info: Dict) -> None:
        self.device_info.update({k: v for k, v in info.items() if v not in (None, "")})
        self._render_device()

    def _render_device(self) -> None:
        d = self.device_info
        facts = d.get("facts") or {}
        values = {
            "name": " / ".join(dict.fromkeys(x for x in (d.get("name"), d.get("hostname")) if x)) or "—",
            "mac": d.get("mac") or "—",
            "link_ip": ("%s（端口 %s）" % (d["link_ip"], d.get("port", 21118))) if d.get("link_ip") else "—",
            "password": (d.get("password") if self.show_pw.isChecked() else "●●●●●●●●") if d.get("password") else "—",
            "rustdesk_version": d.get("rustdesk_version") or "—",
            "configured_at": d.get("configured_at") or "—",
            "system": "—",
            "display": "—",
        }
        if facts:
            values["system"] = "%s · %s" % (facts.get("os", {}).get("pretty", "?"), facts.get("arch", "?"))
            disp = facts.get("display", {})
            values["display"] = "显示器：%s；已登录用户：%d" % (
                "、".join(disp.get("monitors") or []) or "未连接", disp.get("user_sessions", 0))
        for k, v in values.items():
            self.dev_fields[k].setText(v)
        has = bool(d.get("link_ip") and d.get("password"))
        self.copy_ip.setEnabled(bool(d.get("link_ip")))
        self.copy_pw.setEnabled(bool(d.get("password")))
        self.open_rd.setEnabled(has)

    def _copy(self, key: str) -> None:
        v = self.device_info.get(key)
        if v:
            QGuiApplication.clipboard().setText(v)
            self.statusBar().showMessage("已复制", 2000)

    def open_rustdesk(self) -> None:
        prefix = rustdesk_local.find_client(self.settings.rustdesk_client)
        if not prefix:
            QMessageBox.warning(self, APP_NAME, "本机没有找到 RustDesk 客户端，请先安装，或在设置里指定程序位置。")
            return
        rustdesk_local.launch(prefix, self.device_info["link_ip"], self.device_info["password"])

    def _on_request(self, req: Request) -> None:
        if req.kind == "credentials":
            dlg = CredentialsDialog(self, req.payload["title"], req.payload["username"],
                                    req.payload["error"], self.settings.remember_ssh_password,
                                    self.settings.ssh_key_file)
            req.answer(dlg.credentials() if dlg.exec() else None)
        elif req.kind == "confirm":
            buttons = QMessageBox.Yes | QMessageBox.No
            default = QMessageBox.Yes if req.payload["default"] else QMessageBox.No
            r = QMessageBox.question(self, req.payload["title"], req.payload["text"], buttons, default)
            req.answer(r == QMessageBox.Yes)
        elif req.kind == "choose":
            options = req.payload["options"]
            item, ok = QInputDialog.getItem(self, APP_NAME, req.payload["title"], options, 0, False)
            req.answer(options.index(item) if ok else None)
        else:
            req.answer(None)

    # ------------------------------------------------------------------ 日志

    def log(self, msg: str, level: str = "info") -> None:
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        self.log_lines.append("[%s] %s" % (level, line))
        if level == "debug" and not self.verbose.isChecked():
            return
        color = LEVEL_COLOR.get(level)
        style = " style='color:%s'" % color if color else ""
        # 总是包一层 span，让 Qt 按富文本处理，转义字符才能正确显示
        self.log_view.append("<span%s>%s</span>" % (style, html.escape(line, quote=False)))
        self.log_view.moveCursor(QTextCursor.End)

    def export_log(self) -> None:
        default = os.path.join(log_dir(), "autorustdesk-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", default, "日志 (*.log *.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.log_lines) + "\n")
            self.statusBar().showMessage("日志已保存到 %s" % path, 5000)

    # ------------------------------------------------------------------ 其它

    def open_settings(self) -> None:
        if SettingsDialog(self, self.settings).exec():
            self.refresh_nics()

    def open_devices(self) -> None:
        DevicesDialog(self, self.registry).exec()

    def about(self) -> None:
        QMessageBox.about(self, "关于 %s" % APP_NAME, (
            "<b>%s %s</b><br>用网线直连电脑 B（Ubuntu 20.04），自动组网、离线部署并配置 RustDesk，"
            "然后以 IP 直连方式远程控制。<br><br>RustDesk 是 RustDesk 团队的开源软件（AGPL-3.0），"
            "本工具只分发官方原版安装包。") % (APP_NAME, __version__))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 方法名
        busy = self.runner is not None and self.runner.isRunning()
        if busy and not self.closing:
            if QMessageBox.question(self, APP_NAME, "操作正在进行，确定要退出吗？") != QMessageBox.Yes:
                event.ignore()
                return
            self.closing = True
            self.ui.cancel()
            event.ignore()
            return
        if busy:
            event.ignore()
            return
        if self.workflow.helper.running and not self.closing:
            self.closing = True
            self.log("正在恢复本机网络……")
            self.restore_network()
            event.ignore()
            return
        self.settings.save()
        event.accept()

