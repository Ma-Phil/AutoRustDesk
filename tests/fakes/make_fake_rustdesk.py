"""生成测试用的假 RustDesk .deb：依赖列表、安装脚本与官方包一致，程序换成 fake_rustdesk.py。"""

import os
import sys

from tests.fakes.debwriter import build_deb

HERE = os.path.dirname(os.path.abspath(__file__))

# 与 RustDesk build.py 中 generate_control_file() 相同（x86_64）
CONTROL = """Package: rustdesk
Section: net
Priority: optional
Version: {version}
Architecture: amd64
Maintainer: rustdesk <info@rustdesk.com>
Homepage: https://rustdesk.com
Depends: libgtk-3-0t64 | libgtk-3-0, libxcb-randr0, libxdo3 | libxdo4, libxfixes3, libxcb-shape0, libxcb-xfixes0, libasound2t64 | libasound2, libsystemd0, curl, libva2, libva-drm2, libva-x11-2, libgstreamer-plugins-base1.0-0, gstreamer1.0-pipewire
Recommends: libayatana-appindicator3-1
Description: A remote control software.
"""

SERVICE = """[Unit]
Description=RustDesk
Requires=network.target
After=systemd-user-sessions.service

[Service]
Type=simple
ExecStart=/usr/bin/rustdesk --service
ExecStop=pkill -f "rustdesk --"
PIDFile=/run/rustdesk.pid
KillMode=mixed
TimeoutStopSec=30
User=root
LimitNOFILE=100000

[Install]
WantedBy=multi-user.target
"""

# 与官方 res/DEBIAN/postinst 一致
POSTINST = """#!/bin/bash

set -e

if [ "$1" = configure ]; then

	INITSYS=$(ls -al /proc/1/exe | awk -F' ' '{print $NF}' | awk -F'/' '{print $NF}')
	ln -f -s /usr/share/rustdesk/rustdesk /usr/bin/rustdesk

	if [ "systemd" == "$INITSYS" ]; then

		if [ -e /etc/systemd/system/rustdesk.service ]; then
			rm -f /etc/systemd/system/rustdesk.service /usr/lib/systemd/system/rustdesk.service /usr/lib/systemd/user/rustdesk.service >/dev/null  2>&1
		fi
		mkdir -p /usr/lib/systemd/system/
		cp /usr/share/rustdesk/files/systemd/rustdesk.service /usr/lib/systemd/system/rustdesk.service
		systemctl daemon-reload
		systemctl enable rustdesk
		systemctl start rustdesk
	fi
fi
"""

PRERM = """#!/bin/bash
set -e
case $1 in
    remove|upgrade)
		rm -f /usr/bin/rustdesk
        ;;
esac
exit 0
"""


def make(path: str, version: str = "1.4.2") -> str:
    with open(os.path.join(HERE, "fake_rustdesk.py"), "rb") as f:
        binary = f.read()
    files = {
        "usr/share/rustdesk/rustdesk": (binary, 0o755),
        "usr/share/rustdesk/VERSION": (version.encode() + b"\n", 0o644),
        "usr/share/rustdesk/files/systemd/rustdesk.service": (SERVICE.encode(), 0o644),
    }
    return build_deb(
        path, CONTROL.format(version=version), files, {"postinst": POSTINST, "prerm": PRERM}
    )


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "rustdesk-fake-x86_64.deb"
    ver = sys.argv[2] if len(sys.argv) > 2 else "1.4.2"
    print(make(out, ver))
