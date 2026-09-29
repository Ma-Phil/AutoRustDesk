#!/bin/bash
# 在 Ubuntu 上打包成免安装程序：dist/AutoRustDesk-linux-x86_64.tar.gz
# 用法：bash packaging/build_linux.sh
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv-build
. .venv-build/bin/activate
pip install -q -r requirements.txt pyinstaller

VERSION=$(python3 -c "import autorustdesk; print(autorustdesk.__version__)")
pyinstaller --noconfirm --clean --name AutoRustDesk --windowed \
    --paths . \
    --collect-submodules autorustdesk \
    --add-data "autorustdesk/remote/ard_remote.py:autorustdesk/remote" \
    --exclude-module tkinter \
    packaging/entry.py

cp packaging/autorustdesk.desktop packaging/install_desktop_entry.sh dist/AutoRustDesk/
tar -C dist -czf "dist/AutoRustDesk-${VERSION}-linux-x86_64.tar.gz" AutoRustDesk
echo "完成：dist/AutoRustDesk-${VERSION}-linux-x86_64.tar.gz"
