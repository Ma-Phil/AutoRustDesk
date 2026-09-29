"""程序自带的离线包和 RustDesk 客户端。"""

import sys

import pytest

from autorustdesk.core import builtin, rustdesk_local
from autorustdesk.core.devices import DeviceRegistry
from autorustdesk.core.settings import Settings
from autorustdesk.core.workflow import Ui, Workflow


@pytest.fixture()
def resources(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORUSTDESK_RESOURCES", str(tmp_path))
    monkeypatch.setenv("AUTORUSTDESK_CONFIG_DIR", str(tmp_path / "cfg"))
    (tmp_path / "bundle").mkdir()
    (tmp_path / "rustdesk").mkdir()
    return tmp_path


def test_nothing_builtin(resources):
    assert builtin.builtin_bundle() == ""
    if sys.platform != "darwin":
        assert builtin.builtin_client() is None


def test_builtin_bundle_and_client(resources, monkeypatch):
    (resources / "bundle" / "autorustdesk-bundle-rustdesk-1.4.2-ubuntu-20.04-22.04-24.04.tar").write_bytes(b"x")
    assert builtin.builtin_bundle().endswith("rustdesk-1.4.2-ubuntu-20.04-22.04-24.04.tar")
    monkeypatch.setattr(builtin.sys, "platform", "linux")
    appimage = resources / "rustdesk" / "rustdesk-1.4.2-x86_64.AppImage"
    appimage.write_bytes(b"x")
    assert builtin.builtin_client() == [str(appimage), "--appimage-extract-and-run"]
    monkeypatch.setattr(builtin.sys, "platform", "win32")
    exe = resources / "rustdesk" / "rustdesk-1.4.2-x86_64.exe"
    exe.write_bytes(b"x")
    assert builtin.builtin_client() == [str(exe)]


def test_find_client_falls_back_to_builtin(resources, monkeypatch):
    monkeypatch.setattr(rustdesk_local.shutil, "which", lambda name: None)
    monkeypatch.setattr(rustdesk_local, "LINUX_CANDIDATES", [])
    monkeypatch.setattr(rustdesk_local.sys, "platform", "linux")
    monkeypatch.setattr(builtin.sys, "platform", "linux")
    assert rustdesk_local.find_client() is None
    appimage = resources / "rustdesk" / "rustdesk-1.4.2-x86_64.AppImage"
    appimage.write_bytes(b"x")
    assert rustdesk_local.find_client() == [str(appimage), "--appimage-extract-and-run"]


def test_workflow_uses_builtin_bundle_when_none_selected(resources, monkeypatch):
    tar = resources / "bundle" / "b.tar"
    tar.write_bytes(b"x")
    opened = []
    monkeypatch.setattr("autorustdesk.core.workflow.Bundle.open", lambda path: opened.append(path) or path)
    wf = Workflow(Settings(), DeviceRegistry(str(resources / "d.json")), Ui())
    assert wf.bundle() == str(tar)
    # 选过的离线包被删掉了：改用自带的
    wf.settings.bundle_path = str(resources / "gone.tar")
    assert wf.bundle() == str(tar)
    # 选的离线包还在：用选的
    mine = resources / "mine.tar"
    mine.write_bytes(b"x")
    wf.settings.bundle_path = str(mine)
    assert wf.bundle() == str(mine)


@pytest.mark.parametrize("orig", [None, "/opt/user-libs"])
def test_frozen_linux_restores_ld_library_path(monkeypatch, orig):
    """打包版不能把自带库的目录传给子进程（nmcli、RustDesk 会加载到旧版库）。"""
    from autorustdesk import __main__ as entry

    monkeypatch.setattr(entry.sys, "frozen", True, raising=False)
    monkeypatch.setattr(entry.sys, "platform", "linux")
    bundled = "/opt/autorustdesk/_internal"
    monkeypatch.setenv("LD_LIBRARY_PATH", bundled + (":" + orig if orig else ""))
    if orig:
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", orig)
    else:
        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    entry._restore_child_env()
    assert entry.os.environ.get("LD_LIBRARY_PATH") == orig
    assert "LD_LIBRARY_PATH_ORIG" not in entry.os.environ


def test_source_run_keeps_ld_library_path(monkeypatch):
    from autorustdesk import __main__ as entry

    monkeypatch.delattr(entry.sys, "frozen", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/x")
    entry._restore_child_env()
    assert entry.os.environ["LD_LIBRARY_PATH"] == "/opt/x"
