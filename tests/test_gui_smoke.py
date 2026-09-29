import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_main_window_and_dialogs(app):
    from autorustdesk.gui.dialogs import BundleDialog, CredentialsDialog, DevicesDialog, SettingsDialog
    from autorustdesk.gui.main_window import MainWindow

    w = MainWindow()
    w._on_step("network", "done", "eth0 = 192.168.77.1/24")
    w._on_device({"mac": "52:54:00:00:00:01", "hostname": "robot", "name": "robot",
                  "link_ip": "192.168.77.100", "password": "abc123XYZ"})
    assert w.dev_fields["name"].text() == "robot"
    assert w.dev_fields["password"].text() != "abc123XYZ"
    w.show_pw.setChecked(True)
    assert w.dev_fields["password"].text() == "abc123XYZ"
    w.log("RustDesk whitelist：（空） → 192.168.77.0/24 <'&'>")
    assert "<'&'>" in w.log_view.toPlainText()
    SettingsDialog(w, w.settings)
    CredentialsDialog(w, "登录", "robot", "密码不对", False, "")
    BundleDialog(w, w.settings)
    DevicesDialog(w, w.registry)
    w.workflow.helper.stop()
