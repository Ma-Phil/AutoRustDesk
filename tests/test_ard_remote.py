import pytest

from autorustdesk.remote import ard_remote as R

UBUNTU_2004_GDM = """\
# GDM configuration storage
#
# See /usr/share/gdm/gdm.schemas for a list of available options.

[daemon]
# Uncomment the line below to force the login screen to use Xorg
#WaylandEnable=false

# Enabling automatic login
#  AutomaticLoginEnable = true
#  AutomaticLogin = user1

[security]

[xdmcp]

[chooser]

[debug]
# Uncomment the line below to turn on debugging
#Enable=true
"""


def test_gdm_default_is_seen_as_wayland_by_rustdesk():
    assert R.rustdesk_sees_login_wayland(UBUNTU_2004_GDM)
    assert R.gdm_wayland_state(UBUNTU_2004_GDM) == "enabled"


def test_gdm_disable_wayland_default_file():
    new, changed = R.gdm_disable_wayland(UBUNTU_2004_GDM)
    assert changed
    assert not R.rustdesk_sees_login_wayland(new)
    assert R.gdm_wayland_state(new) == "disabled"
    lines = new.splitlines()
    assert lines[lines.index("[daemon]") + 1] == "WaylandEnable=false"
    # 其它设置保持不变
    assert "#  AutomaticLogin = user1" in new
    assert "[debug]" in new and "#Enable=true" in new
    # 再改一次没有变化（幂等）
    again, changed2 = R.gdm_disable_wayland(new)
    assert not changed2 and again == new


@pytest.mark.parametrize(
    "text",
    [
        "[daemon]\nWaylandEnable=true\n",
        "[daemon]\nWaylandEnable = true\nWaylandEnable=false\n",
        "[security]\nWaylandEnable=true\n",
        "",
        "[debug]\nEnable=true\n",
        "[daemon]\n# WaylandEnable = false\nAutomaticLoginEnable=true\n",
    ],
)
def test_gdm_disable_wayland_variants(text):
    new, _ = R.gdm_disable_wayland(text)
    assert R.gdm_wayland_state(new) == "disabled"
    assert not R.rustdesk_sees_login_wayland(new)
    assert new.count("WaylandEnable") == 1


# 参考值来自 Ubuntu 20.04 上的 `cvt W H 60`
@pytest.mark.parametrize(
    "w,h,expected",
    [
        (1920, 1080, '"1920x1080" 173.00 1920 2048 2248 2576 1080 1083 1088 1120 -hsync +vsync'),
        (1366, 768, '"1368x768" 85.25 1368 1440 1576 1784 768 771 781 798 -hsync +vsync'),
        (1280, 1024, '"1280x1024" 109.00 1280 1368 1496 1712 1024 1027 1034 1063 -hsync +vsync'),
        (1600, 900, '"1600x900" 118.25 1600 1696 1856 2112 900 903 908 934 -hsync +vsync'),
        (2560, 1440, '"2560x1440" 312.25 2560 2752 3024 3488 1440 1443 1448 1493 -hsync +vsync'),
        (1280, 720, '"1280x720" 74.50 1280 1344 1472 1664 720 723 728 748 -hsync +vsync'),
        (1024, 768, '"1024x768" 63.50 1024 1072 1176 1328 768 771 775 798 -hsync +vsync'),
    ],
)
def test_cvt_modeline_matches_xserver_cvt(w, h, expected):
    name, line = R.cvt_modeline(w, h)
    assert line == expected
    assert name == expected.split('"')[1]


def test_dummy_xorg_conf():
    conf = R.dummy_xorg_conf(1920, 1080)
    assert 'Driver     "dummy"' in conf
    assert 'Modes   "1920x1080"' in conf
    assert "Virtual 1920 1080" in conf


def test_parse_resolution():
    assert R.parse_resolution("1920x1080") == (1920, 1080)
    assert R.parse_resolution(" 1280 X 720 ") == (1280, 720)
    with pytest.raises(R.ArdError):
        R.parse_resolution("abc")
    with pytest.raises(R.ArdError):
        R.parse_resolution("100x100")


def test_merge_whitelist():
    assert R.merge_whitelist("", "192.168.77.0/24") == "192.168.77.0/24"
    assert R.merge_whitelist("10.0.0.0/8", "192.168.77.0/24") == "10.0.0.0/8,192.168.77.0/24"
    assert R.merge_whitelist("192.168.77.0/24", "192.168.77.0/24") == "192.168.77.0/24"


def test_parse_listen_ports():
    text = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:52FE 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 1
   1: 0100007F:0016 0100007F:9C40 01 00000000:00000000 00:00000000 00000000     0        0 2 1
   2: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 3 1
"""
    assert R.parse_listen_ports(text) == {21246, 22}


def test_parse_loginctl_show():
    props = R.parse_loginctl_show("Name=gdm\nType=x11\nClass=greeter\nState=active\n")
    assert props["Class"] == "greeter"
    sess = [props, {"Class": "user", "Type": "x11", "State": "active"},
            {"Class": "user", "Type": "tty", "State": "active"}]
    assert len(R.graphical_user_sessions(sess)) == 1
    assert len(R.greeter_sessions(sess)) == 1


def test_display_unit_points_to_installed_script():
    unit = R.display_unit_text("/usr/bin/python3")
    assert "ExecStart=/usr/bin/python3 %s display-switch --boot" % R.INSTALLED_SCRIPT in unit
    assert "Before=display-manager.service" in unit


def test_xorg_usable_by_release_and_sessions():
    # 20.04/22.04/24.04：有 Xorg 会话，可以改用 Xorg
    assert R.xorg_usable("focal", ["ubuntu.desktop"])
    assert R.xorg_usable("noble", ["ubuntu.desktop", "ubuntu-xorg.desktop"])
    # 26.04：GNOME 不再提供 Xorg 会话，哪怕装了别的 X 桌面也不改登录界面
    assert not R.xorg_usable("resolute", ["gnome-flashback-metacity.desktop"])
    # 未知的新版本：看有没有 Xorg 会话
    assert not R.xorg_usable("zesty-future", [])
    assert R.xorg_usable("zesty-future", ["xfce.desktop"])
