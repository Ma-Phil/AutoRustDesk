# -*- mode: python -*-
# PyInstaller 打包配置（Windows / macOS），由 packaging/build.py 调用。
# Linux 版用 packaging/build_linux.sh。
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - PyInstaller 提供 SPECPATH
sys.path.insert(0, ROOT)
from autorustdesk import APP_NAME, __version__  # noqa: E402

# macOS：电脑 B 的离线包放进 .app 里面（必须在签名之前），由 packaging/build.py 传入
EXTRA = []
if os.environ.get("AUTORUSTDESK_BUNDLE"):
    EXTRA.append((os.environ["AUTORUSTDESK_BUNDLE"], "bundle"))
    EXTRA.append((os.path.join(ROOT, "packaging", "THIRD_PARTY_NOTICES.txt"), "."))

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    hiddenimports=collect_submodules("autorustdesk"),
    datas=[(os.path.join(ROOT, "autorustdesk", "remote", "ard_remote.py"), "autorustdesk/remote"),
           (os.path.join(ROOT, "autorustdesk", "gui", "autorustdesk.png"), "autorustdesk/gui")] + EXTRA,
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)  # noqa: F821
ICON = os.path.join(ROOT, "packaging", "icons", "autorustdesk.ico" if sys.platform == "win32" else "autorustdesk.icns")

# 图形界面程序（也用来以管理员身份运行网络助手：AutoRustDesk helper ...）
gui = EXE(  # noqa: F821
    pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME, console=False,
    upx=False, argv_emulation=False, icon=ICON,
)
programs = [gui]
if sys.platform == "win32":
    # Windows 的窗口程序没有命令行输出，另外提供一个命令行版（制作离线包、命令行连接用）
    programs.append(EXE(  # noqa: F821
        pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME + "-cli", console=True, upx=False,
        icon=ICON,
    ))

coll = COLLECT(*programs, a.binaries, a.datas, name=APP_NAME, upx=False)  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name=APP_NAME + ".app",
        bundle_identifier="io.github.ma-phil.autorustdesk",
        icon=ICON,
        version=__version__,
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
