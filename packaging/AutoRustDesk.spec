# -*- mode: python -*-
# PyInstaller 打包配置（Windows / macOS），由 packaging/build.py 调用。
# Linux 版用 packaging/build_linux.sh。
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - PyInstaller 提供 SPECPATH
sys.path.insert(0, ROOT)
from autorustdesk import APP_NAME, __version__  # noqa: E402

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    hiddenimports=collect_submodules("autorustdesk"),
    datas=[(os.path.join(ROOT, "autorustdesk", "remote", "ard_remote.py"), "autorustdesk/remote")],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)  # noqa: F821

# 图形界面程序（也用来以管理员身份运行网络助手：AutoRustDesk helper ...）
gui = EXE(  # noqa: F821
    pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME, console=False,
    upx=False, argv_emulation=False,
)
programs = [gui]
if sys.platform == "win32":
    # Windows 的窗口程序没有命令行输出，另外提供一个命令行版（制作离线包、命令行连接用）
    programs.append(EXE(  # noqa: F821
        pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME + "-cli", console=True, upx=False,
    ))

coll = COLLECT(*programs, a.binaries, a.datas, name=APP_NAME, upx=False)  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name=APP_NAME + ".app",
        bundle_identifier="io.github.ma-phil.autorustdesk",
        version=__version__,
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
