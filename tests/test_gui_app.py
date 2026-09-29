"""启动前的界面环境检查（不需要 PySide6）。"""

import pytest

from autorustdesk.gui import app


@pytest.fixture()
def env(monkeypatch):
    for k in ("QT_QPA_PLATFORM", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(app.sys, "platform", "linux")
    return monkeypatch


def test_x11_without_xcb_cursor_gives_install_hint(env):
    env.setenv("DISPLAY", ":0")
    env.setattr(app, "_loadable", lambda name: False)
    msg = app.check_qt_platform()
    assert msg is not None and "sudo apt install libxcb-cursor0" in msg


def test_x11_with_xcb_cursor_is_fine(env):
    env.setenv("DISPLAY", ":0")
    env.setattr(app, "_loadable", lambda name: True)
    assert app.check_qt_platform() is None


def test_wayland_without_xcb_cursor_falls_back_to_wayland(env):
    env.setenv("DISPLAY", ":0")
    env.setenv("WAYLAND_DISPLAY", "wayland-0")
    env.setattr(app, "_loadable", lambda name: False)
    assert app.check_qt_platform() is None
    assert app.os.environ["QT_QPA_PLATFORM"] == "wayland"


def test_no_display_at_all(env):
    env.setattr(app, "_loadable", lambda name: True)
    msg = app.check_qt_platform()
    assert msg is not None and "connect" in msg


def test_explicit_non_xcb_platform_is_not_checked(env):
    env.setenv("QT_QPA_PLATFORM", "offscreen")
    env.setattr(app, "_loadable", lambda name: False)
    assert app.check_qt_platform() is None


def test_loadable_prefers_bundled_copy(env, tmp_path):
    tried = []

    def fake_cdll(path):
        tried.append(path)
        raise OSError("nope")

    env.setattr(app.ctypes, "CDLL", fake_cdll)
    env.setattr(app.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert app._loadable("libxcb-cursor.so.0") is False
    assert tried == [str(tmp_path / "libxcb-cursor.so.0"), "libxcb-cursor.so.0"]
