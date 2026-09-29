#!/bin/bash
# 在 Ubuntu 上打包成免安装程序：dist/AutoRustDesk-<版本>-linux-x86_64.tar.gz
# 用法：bash packaging/build_linux.sh
#
# 打包机上必须装有下面检查的系统库：PyInstaller 会把它们一起放进程序包，
# 这样程序拿到别的电脑上（即使那台电脑没装这些库）也能直接运行。
set -euo pipefail
cd "$(dirname "$0")/.."

# ---------------------------------------------------------------- 打包前检查
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
LDCONFIG=$(command -v ldconfig || echo /sbin/ldconfig)
LIBS=$("$LDCONFIG" -p)
# 共享库 -> 所在的软件包。libxcb-cursor0 是 Qt 6.5+ 的 X11 界面必需的，Ubuntu 默认没装。
REQUIRED="
libpython${PYVER}.so.1.0:libpython${PYVER}
libxcb-cursor.so.0:libxcb-cursor0
libxcb-icccm.so.4:libxcb-icccm4
libxcb-image.so.0:libxcb-image0
libxcb-keysyms.so.1:libxcb-keysyms1
libxcb-render-util.so.0:libxcb-render-util0
libxcb-xinerama.so.0:libxcb-xinerama0
libxcb-xkb.so.1:libxcb-xkb1
libxcb-randr.so.0:libxcb-randr0
libxcb-shape.so.0:libxcb-shape0
libxkbcommon-x11.so.0:libxkbcommon-x11-0
"
missing=""
for item in $REQUIRED; do
    lib=${item%%:*}
    pkg=${item#*:}
    if ! grep -qF "$lib (" <<< "$LIBS"; then
        missing="$missing $pkg"
    fi
done
if ! python3 -c 'import ensurepip' 2>/dev/null; then
    missing="$missing python3-venv"
fi
if [ -n "$missing" ]; then
    echo "打包机缺少以下软件包，请先安装后再打包：" >&2
    echo "    sudo apt install$missing" >&2
    exit 1
fi

# ---------------------------------------------------------------- 打包
python3 -m venv .venv-build
. .venv-build/bin/activate
# Ubuntu 20.04 自带的 pip 20.0 不认识 manylinux_2_28 格式，装不上新版 PySide6
pip install -q --upgrade pip
pip install -q -r requirements.txt pyinstaller

VERSION=$(python3 -c "import autorustdesk; print(autorustdesk.__version__)")
pyinstaller --noconfirm --clean --name AutoRustDesk --windowed \
    --paths . \
    --collect-submodules autorustdesk \
    --add-data "autorustdesk/remote/ard_remote.py:autorustdesk/remote" \
    --add-data "autorustdesk/gui/autorustdesk.png:autorustdesk/gui" \
    --exclude-module tkinter \
    packaging/entry.py

# ---------------------------------------------------------------- 打包后检查
if [ -z "$(find dist/AutoRustDesk -name 'libxcb-cursor.so.0' -print -quit)" ]; then
    echo "错误：程序包里没有 libxcb-cursor.so.0，在没装 libxcb-cursor0 的电脑上会无法启动" >&2
    exit 1
fi
dist/AutoRustDesk/AutoRustDesk bundle --help > /dev/null

cp packaging/autorustdesk.desktop packaging/install_desktop_entry.sh dist/AutoRustDesk/
tar -C dist -czf "dist/AutoRustDesk-${VERSION}-linux-x86_64.tar.gz" AutoRustDesk
echo "完成：dist/AutoRustDesk-${VERSION}-linux-x86_64.tar.gz"
