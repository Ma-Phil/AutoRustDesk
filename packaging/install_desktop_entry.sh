#!/bin/bash
# 在应用菜单里添加 AutoRustDesk（当前用户）。
# 打包版：在解压目录里运行；源码版：在仓库根目录运行 bash packaging/install_desktop_entry.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -x "$HERE/AutoRustDesk" ]; then
    EXEC="$HERE/AutoRustDesk"
    DESKTOP="$HERE/autorustdesk.desktop"
else
    ROOT="$(cd "$HERE/.." && pwd)"
    PY="$ROOT/.venv/bin/python3"
    [ -x "$PY" ] || PY="$(command -v python3)"
    EXEC="env PYTHONPATH=$ROOT $PY -m autorustdesk"
    DESKTOP="$HERE/autorustdesk.desktop"
fi
mkdir -p "$HOME/.local/share/applications"
sed "s|@EXEC@|$EXEC|" "$DESKTOP" > "$HOME/.local/share/applications/autorustdesk.desktop"
echo "已添加到应用菜单：AutoRustDesk"
