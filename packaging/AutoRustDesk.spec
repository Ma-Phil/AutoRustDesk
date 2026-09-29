# -*- mode: python -*-
# PyInstaller 打包配置（Windows / macOS），由 packaging/build.py 调用。
# Linux 版用 packaging/build_linux.sh。
import os
import re
import subprocess
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


def qt_min_macos() -> str:
    """打包进去的 Qt 支持的最低 macOS 版本。

    写进 Info.plist 后，在更旧的系统上打开时系统会直接提示版本太低，而不是闪退。
    先读 QtCore 本身的 LC_BUILD_VERSION；读不到时看 PySide6 安装包标注的系统版本（如 macosx_13_0）。
    """
    import glob
    from importlib import metadata

    import PySide6

    def newest(versions):
        return max(versions, key=lambda v: tuple(int(x) for x in v.split(".")))

    framework = os.path.join(os.path.dirname(PySide6.__file__), "Qt", "lib", "QtCore.framework")
    found = []
    for binary in glob.glob(os.path.join(framework, "**", "QtCore"), recursive=True):
        if "Headers" in binary or not os.path.isfile(binary):
            continue
        try:
            out = subprocess.run(["otool", "-l", binary], capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError) as e:
            print("otool 读取 %s 失败：%s" % (binary, e))
            continue
        found += re.findall(r"cmd LC_BUILD_VERSION.*?minos (\d+(?:\.\d+)*)", out, re.S)
    if found:
        print("Qt 支持的最低 macOS 版本（QtCore）：%s" % newest(found))
        return newest(found)
    for dist in ("PySide6_Essentials", "PySide6", "shiboken6"):
        try:
            wheel = metadata.distribution(dist).read_text("WHEEL") or ""
        except metadata.PackageNotFoundError:
            continue
        found += ["%s.%s" % m for m in re.findall(r"macosx_(\d+)_(\d+)_", wheel)]
    if found:
        print("Qt 支持的最低 macOS 版本（PySide6 安装包）：%s" % newest(found))
        return newest(found)
    print("警告：读不到 Qt 支持的最低 macOS 版本，按 13.0 处理")
    return "13.0"


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
            "LSMinimumSystemVersion": qt_min_macos(),
        },
    )
