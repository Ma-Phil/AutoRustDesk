"""对话框：登录、设置、制作离线包、设备列表。"""

import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..bundle.builder import DEFAULT_MIRROR, MIRRORS, default_output_name
from ..core.devices import DeviceRegistry
from ..core.settings import Settings
from ..core.workflow import Credentials
from .bridge import BundleBuildRunner


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("color: #666666;")
    return lab


class CredentialsDialog(QDialog):
    def __init__(self, parent: QWidget, title: str, username: str, error: str,
                 remember: bool, key_file: str):
        super().__init__(parent)
        self.setWindowTitle("登录电脑 B")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        head = QLabel(title)
        head.setStyleSheet("font-weight: bold;")
        lay.addWidget(head)
        if error:
            err = QLabel(error)
            err.setWordWrap(True)
            err.setStyleSheet("color: #c62828;")
            lay.addWidget(err)
        form = QFormLayout()
        self.user = QLineEdit(username)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.remember = QCheckBox("记住密码（保存在本机，仅当前用户可读）")
        self.remember.setChecked(remember)
        self.key = QLineEdit(key_file)
        self.key.setPlaceholderText("可选：SSH 私钥文件")
        key_row = QHBoxLayout()
        key_row.addWidget(self.key)
        browse = QPushButton("选择…")
        browse.clicked.connect(self._browse)
        key_row.addWidget(browse)
        form.addRow("用户名", self.user)
        form.addRow("密码", self.password)
        form.addRow("私钥", key_row)
        lay.addLayout(form)
        lay.addWidget(self.remember)
        lay.addWidget(_hint("需要 B 上一个可以使用 sudo 的账号。同型号设备通常共用同一个账号。"))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("登录")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        (self.password if username else self.user).setFocus()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 SSH 私钥", os.path.expanduser("~/.ssh"))
        if path:
            self.key.setText(path)

    def credentials(self) -> Credentials:
        return Credentials(self.user.text().strip(), self.password.text(), self.key.text().strip(),
                           self.remember.isChecked())


class SettingsDialog(QDialog):
    HEADLESS = [("auto", "自动（没接显示器时启用虚拟显示器）"), ("on", "始终使用虚拟显示器"),
                ("off", "不使用虚拟显示器"), ("skip", "不修改显示设置")]

    def __init__(self, parent: QWidget, settings: Settings):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        # 网络
        w = QWidget()
        f = QFormLayout(w)
        self.subnet = QLineEdit(settings.subnet)
        self.lease = QSpinBox()
        self.lease.setRange(300, 86400)
        self.lease.setSuffix(" 秒")
        self.lease.setValue(settings.lease_time)
        self.timeout = QSpinBox()
        self.timeout.setRange(30, 1800)
        self.timeout.setSuffix(" 秒")
        self.timeout.setValue(settings.discover_timeout)
        self.virtual = QCheckBox("在网卡列表中显示虚拟网卡")
        self.virtual.setChecked(settings.show_virtual_nics)
        f.addRow("直连网段", self.subnet)
        f.addRow("", _hint("本机使用第一个地址（如 192.168.77.1），B 通过 DHCP 获得 .100–.199。"
                           "与本机其它网络冲突时会自动换一个网段。"))
        f.addRow("DHCP 租期", self.lease)
        f.addRow("查找 B 的超时", self.timeout)
        f.addRow("", self.virtual)
        tabs.addTab(w, "网络")

        # 电脑 B
        w = QWidget()
        f = QFormLayout(w)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(settings.rustdesk_port)
        self.whitelist = QCheckBox("只允许直连网段连接 B 的 RustDesk（IP 白名单）")
        self.whitelist.setChecked(settings.whitelist)
        self.headless = QComboBox()
        for key, label in self.HEADLESS:
            self.headless.addItem(label, key)
        self.headless.setCurrentIndex(max(0, self.headless.findData(settings.headless)))
        self.resolution = QComboBox()
        self.resolution.setEditable(True)
        for r in ("1280x720", "1366x768", "1600x900", "1920x1080", "2560x1440"):
            self.resolution.addItem(r)
        self.resolution.setCurrentText(settings.resolution)
        self.sleep = QCheckBox("禁止 B 自动休眠（否则登录界面闲置约 20 分钟后会休眠失联）")
        self.sleep.setChecked(settings.prevent_sleep)
        self.ssh_port = QSpinBox()
        self.ssh_port.setRange(1, 65535)
        self.ssh_port.setValue(settings.ssh_port)
        self.key = QLineEdit(settings.ssh_key_file)
        self.key.setPlaceholderText("可选")
        f.addRow("RustDesk 直连端口", self.port)
        f.addRow("", self.whitelist)
        f.addRow("虚拟显示器", self.headless)
        f.addRow("虚拟显示器分辨率", self.resolution)
        f.addRow("", self.sleep)
        f.addRow("SSH 端口", self.ssh_port)
        f.addRow("SSH 私钥", self.key)
        tabs.addTab(w, "电脑 B")

        # 密码
        w = QWidget()
        f = QFormLayout(w)
        self.pw_mode = QComboBox()
        self.pw_mode.addItem("每台设备随机生成（推荐）", "per_device")
        self.pw_mode.addItem("所有设备使用统一密码", "fixed")
        self.pw_mode.setCurrentIndex(0 if settings.password_mode != "fixed" else 1)
        self.fixed = QLineEdit(settings.fixed_password)
        self.fixed.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.remember = QCheckBox("记住 B 的 SSH 密码")
        self.remember.setChecked(settings.remember_ssh_password)
        f.addRow("RustDesk 密码", self.pw_mode)
        f.addRow("统一密码", self.fixed)
        f.addRow("", _hint("密码保存在本机设备记录中，连接时自动填写。"))
        f.addRow("", self.remember)
        tabs.addTab(w, "密码")

        # 本机 RustDesk
        w = QWidget()
        f = QFormLayout(w)
        self.client = QLineEdit(settings.rustdesk_client)
        self.client.setPlaceholderText("留空自动查找（/usr/bin/rustdesk、Flatpak 等）")
        row = QHBoxLayout()
        row.addWidget(self.client)
        browse = QPushButton("选择…")
        browse.clicked.connect(self._browse_client)
        row.addWidget(browse)
        self.launch = QCheckBox("完成后自动打开 RustDesk 连接 B")
        self.launch.setChecked(settings.auto_launch)
        f.addRow("RustDesk 程序", row)
        f.addRow("", self.launch)
        tabs.addTab(w, "本机 RustDesk")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _browse_client(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 RustDesk 程序")
        if path:
            self.client.setText(path)

    def _save(self) -> None:
        s = self.settings
        backup = dict(s.__dict__)
        s.subnet = self.subnet.text().strip()
        s.lease_time = self.lease.value()
        s.discover_timeout = self.timeout.value()
        s.show_virtual_nics = self.virtual.isChecked()
        s.rustdesk_port = self.port.value()
        s.whitelist = self.whitelist.isChecked()
        s.headless = self.headless.currentData()
        s.resolution = self.resolution.currentText().strip()
        s.prevent_sleep = self.sleep.isChecked()
        s.ssh_port = self.ssh_port.value()
        s.ssh_key_file = self.key.text().strip()
        s.password_mode = self.pw_mode.currentData()
        s.fixed_password = self.fixed.text()
        s.remember_ssh_password = self.remember.isChecked()
        s.rustdesk_client = self.client.text().strip()
        s.auto_launch = self.launch.isChecked()
        errors = s.validate()
        if errors:
            s.__dict__.update(backup)
            QMessageBox.warning(self, "设置有误", "\n".join(errors))
            return
        s.save()
        self.accept()


class BundleDialog(QDialog):
    """用下载好的 RustDesk deb 制作离线部署包（需要联网）。"""

    def __init__(self, parent: QWidget, settings: Settings):
        super().__init__(parent)
        self.settings = settings
        self.result_path: Optional[str] = None
        self.runner: Optional[BundleBuildRunner] = None
        self.setWindowTitle("制作离线部署包")
        self.setMinimumSize(640, 480)
        lay = QVBoxLayout(self)
        lay.addWidget(_hint(
            "在能上网的电脑上操作：选择从 RustDesk GitHub Release 页面下载的 "
            "rustdesk-<版本>-x86_64.deb，程序会从 Ubuntu 20.04 软件源下载它的全部依赖，"
            "连同虚拟显示驱动一起打成一个离线包。"))
        form = QFormLayout()
        self.deb = QLineEdit()
        row = QHBoxLayout()
        row.addWidget(self.deb)
        b = QPushButton("选择…")
        b.clicked.connect(self._browse_deb)
        row.addWidget(b)
        form.addRow("RustDesk deb", row)
        self.mirror = QComboBox()
        self.mirror.setEditable(True)
        for name, url in MIRRORS:
            self.mirror.addItem("%s  %s" % (name, url), url)
        current = settings.mirror or DEFAULT_MIRROR
        idx = self.mirror.findData(current)
        if idx >= 0:
            self.mirror.setCurrentIndex(idx)
        else:
            self.mirror.setEditText(current)
        form.addRow("Ubuntu 软件源", self.mirror)
        self.out = QLineEdit()
        row = QHBoxLayout()
        row.addWidget(self.out)
        b = QPushButton("另存为…")
        b.clicked.connect(self._browse_out)
        row.addWidget(b)
        form.addRow("输出文件", row)
        lay.addLayout(form)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.stage = QLabel("")
        lay.addWidget(self.stage)
        lay.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)
        buttons = QDialogButtonBox()
        self.start_btn = buttons.addButton("开始制作", QDialogButtonBox.AcceptRole)
        self.close_btn = buttons.addButton("关闭", QDialogButtonBox.RejectRole)
        self.start_btn.clicked.connect(self._start)
        self.close_btn.clicked.connect(self.reject)
        lay.addWidget(buttons)

    def _mirror_url(self) -> str:
        data = self.mirror.currentData()
        text = self.mirror.currentText().strip()
        if data and text.endswith(data):
            return data
        return text.split()[-1] if text else DEFAULT_MIRROR

    def _browse_deb(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 RustDesk 安装包",
                                              os.path.expanduser("~/Downloads"), "Debian 包 (*.deb)")
        if path:
            self.deb.setText(path)
            if not self.out.text():
                self.out.setText(os.path.join(os.path.dirname(path), default_output_name(path)))

    def _browse_out(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "离线包保存为", self.out.text(), "离线包 (*.tar)")
        if path:
            self.out.setText(path)

    def _start(self) -> None:
        deb, out = self.deb.text().strip(), self.out.text().strip()
        if not deb or not os.path.isfile(deb):
            QMessageBox.warning(self, "制作离线包", "请先选择 RustDesk 的 deb 安装包")
            return
        if not out:
            out = os.path.join(os.path.dirname(deb), default_output_name(deb))
            self.out.setText(out)
        self.settings.mirror = self._mirror_url()
        self.start_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.log.clear()
        self.runner = BundleBuildRunner(deb, out, self.settings.mirror)
        self.runner.log_signal.connect(self.log.appendPlainText)
        self.runner.progress_signal.connect(self._progress)
        self.runner.finished_with.connect(self._done)
        self.runner.start()

    def _progress(self, stage: str, frac: float) -> None:
        self.stage.setText(stage)
        self.bar.setValue(int(frac * 100))

    def _done(self, result: dict) -> None:
        self.start_btn.setEnabled(True)
        self.close_btn.setEnabled(True)
        if result.get("ok"):
            self.result_path = result["path"]
            self.bar.setValue(100)
            QMessageBox.information(self, "制作离线包", "离线包已生成：\n%s" % self.result_path)
            self.accept()
        else:
            self.log.appendPlainText("制作失败：%s" % result.get("error"))
            QMessageBox.critical(self, "制作离线包", "制作失败：\n%s" % result.get("error"))

    def reject(self) -> None:
        if self.runner and self.runner.isRunning():
            return
        super().reject()


class DevicesDialog(QDialog):
    COLUMNS = ["名称", "MAC", "直连地址", "RustDesk", "RustDesk 密码", "配置时间", "上次发现"]

    def __init__(self, parent: QWidget, registry: DeviceRegistry):
        super().__init__(parent)
        self.registry = registry
        self.setWindowTitle("已知设备")
        self.setMinimumSize(820, 360)
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemChanged.connect(self._renamed)
        lay.addWidget(self.table)
        lay.addWidget(_hint("双击「名称」可以给设备起名字。删除记录后，下次连接该设备会重新生成密码。"))
        row = QHBoxLayout()
        delete = QPushButton("删除选中记录")
        delete.clicked.connect(self._delete)
        row.addWidget(delete)
        row.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self._fill()

    def _fill(self) -> None:
        self.table.blockSignals(True)
        devs = self.registry.all()
        self.table.setRowCount(len(devs))
        for r, d in enumerate(devs):
            values = [d.name or d.hostname, d.mac, d.ip, d.rustdesk_version, d.rustdesk_password,
                      d.configured_at, d.last_seen]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c != 0:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setData(Qt.UserRole, d.mac)
                self.table.setItem(r, c, item)
        self.table.blockSignals(False)

    def _renamed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self.registry.update(item.data(Qt.UserRole), name=item.text().strip())

    def _delete(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        if not rows:
            return
        if QMessageBox.question(self, "删除记录", "确定删除选中的 %d 条设备记录？" % len(rows)) != QMessageBox.Yes:
            return
        for r in rows:
            self.registry.remove(self.table.item(r, 1).text())
        self._fill()
