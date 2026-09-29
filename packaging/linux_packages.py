"""把 build_linux.sh 打好的程序（dist/AutoRustDesk）做成 deb 和 AppImage。

    bash packaging/build_linux.sh
    python3 packaging/linux_packages.py [--bundle 离线包.tar] [--client rustdesk-版本-x86_64.AppImage]

输出：
    dist/autorustdesk_<版本>_amd64.deb         装到 /opt/autorustdesk，应用菜单里有图标，命令行为 autorustdesk
    dist/AutoRustDesk-<版本>-x86_64.AppImage   单个文件，加上可执行权限就能运行

给出 --bundle / --client 时一起打包进去（完整离线版），电脑 A 不用联网。
制作 AppImage 需要 appimagetool，没有时自动从 GitHub 下载。
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from autorustdesk import APP_NAME, __version__  # noqa: E402

PKG = "autorustdesk"
APPIMAGETOOL_URL = ("https://github.com/AppImage/appimagetool/releases/download/continuous/"
                    "appimagetool-x86_64.AppImage")
MAINTAINER = "Ma-Phil <139486144+Ma-Phil@users.noreply.github.com>"
DEPENDS = ("pkexec | policykit-1, libgl1, libegl1, libfontconfig1, libxkbcommon0, "
           "libdbus-1-3, libglib2.0-0")
POLKIT_ACTION = "io.github.maphil.autorustdesk.helper"

POLICY = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <vendor>AutoRustDesk</vendor>
  <vendor_url>https://github.com/Ma-Phil/AutoRustDesk</vendor_url>
  <action id="%s">
    <description>Configure the direct Ethernet link</description>
    <description xml:lang="zh_CN">配置直连网卡</description>
    <message>AutoRustDesk needs administrator rights to configure the direct Ethernet link.</message>
    <message xml:lang="zh_CN">AutoRustDesk 需要管理员权限来配置直连网卡。</message>
    <icon_name>autorustdesk</icon_name>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/opt/autorustdesk/AutoRustDesk</annotate>
  </action>
</policyconfig>
""" % POLKIT_ACTION

APPRUN = """#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/autorustdesk/AutoRustDesk" "$@"
"""


def run(cmd, **kw):
    print("+", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, **kw)


def desktop_entry(exec_cmd: str) -> str:
    with open(os.path.join(ROOT, "packaging", "autorustdesk.desktop"), encoding="utf-8") as f:
        text = f.read()
    return text.replace("@EXEC@", exec_cmd).replace("Icon=network-wired", "Icon=autorustdesk")


def write(path: str, text: str, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)


def copy(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def add_extras(app_dir: str, bundle: str, client: str) -> None:
    """把离线包和 RustDesk 客户端放进程序目录（程序会自动找到它们）。"""
    for sub, src in (("bundle", bundle), ("rustdesk", client)):
        target = os.path.join(app_dir, sub)
        shutil.rmtree(target, ignore_errors=True)
        if src:
            dst = os.path.join(target, os.path.basename(src))
            copy(src, dst)
            if sub == "rustdesk":
                os.chmod(dst, 0o755)
    notices = os.path.join(ROOT, "packaging", "THIRD_PARTY_NOTICES.txt")
    copy(notices, os.path.join(app_dir, "THIRD_PARTY_NOTICES.txt"))


def du_kib(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            p = os.path.join(root, name)
            if not os.path.islink(p):
                total += os.path.getsize(p)
    return (total + 1023) // 1024


def build_deb(app_dir: str, out_dir: str, work: str) -> str:
    root = os.path.join(work, "deb")
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(app_dir, os.path.join(root, "opt", PKG), symlinks=True)
    os.makedirs(os.path.join(root, "usr", "bin"))
    os.symlink("/opt/%s/%s" % (PKG, APP_NAME), os.path.join(root, "usr", "bin", PKG))
    write(os.path.join(root, "usr", "share", "applications", PKG + ".desktop"),
          desktop_entry("/opt/%s/%s" % (PKG, APP_NAME)))
    copy(os.path.join(ROOT, "autorustdesk", "gui", "autorustdesk.png"),
         os.path.join(root, "usr", "share", "icons", "hicolor", "256x256", "apps", PKG + ".png"))
    write(os.path.join(root, "usr", "share", "polkit-1", "actions", POLKIT_ACTION + ".policy"), POLICY)
    copy(os.path.join(ROOT, "packaging", "THIRD_PARTY_NOTICES.txt"),
         os.path.join(root, "usr", "share", "doc", PKG, "copyright"))
    control = "\n".join([
        "Package: " + PKG,
        "Version: " + __version__,
        "Architecture: amd64",
        "Maintainer: " + MAINTAINER,
        "Installed-Size: %d" % du_kib(root),
        "Depends: " + DEPENDS,
        "Section: net",
        "Priority: optional",
        "Homepage: https://github.com/Ma-Phil/AutoRustDesk",
        "Description: 网线直连电脑 B，一键离线部署 RustDesk 并远程控制",
        " 插上网线，一键控制设备桌面：不用键盘、鼠标和显示器，也不依赖 WiFi 和局域网。",
        " .",
        " 完整离线版自带电脑 B（Ubuntu 20.04/22.04/24.04）的 RustDesk 离线部署包和",
        " 本机用的 RustDesk 客户端，全程不用联网。",
    ]) + "\n"
    write(os.path.join(root, "DEBIAN", "control"), control)
    for d, _dirs, files in os.walk(root):
        os.chmod(d, 0o755)
        for name in files:
            p = os.path.join(d, name)
            if not os.path.islink(p):
                mode = os.stat(p).st_mode
                os.chmod(p, 0o755 if mode & stat.S_IXUSR else 0o644)
    out = os.path.join(out_dir, "%s_%s_amd64.deb" % (PKG, __version__))
    # 离线包里的 deb 已经压缩过，用 gzip 就够了（快，老版本 dpkg 也认）
    run(["dpkg-deb", "--root-owner-group", "-Zgzip", "--build", root, out])
    return out


def appimagetool(work: str) -> str:
    found = shutil.which("appimagetool")
    if found:
        return found
    path = os.path.join(work, "appimagetool-x86_64.AppImage")
    if not os.path.exists(path):
        print("下载 appimagetool", flush=True)
        with urllib.request.urlopen(APPIMAGETOOL_URL, timeout=120) as r, open(path + ".part", "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(path + ".part", path)
        os.chmod(path, 0o755)
    return path


def build_appimage(app_dir: str, out_dir: str, work: str) -> str:
    appdir = os.path.join(work, "AppDir")
    shutil.rmtree(appdir, ignore_errors=True)
    shutil.copytree(app_dir, os.path.join(appdir, "usr", "lib", PKG), symlinks=True)
    write(os.path.join(appdir, "AppRun"), APPRUN, 0o755)
    entry = desktop_entry(APP_NAME)
    write(os.path.join(appdir, PKG + ".desktop"), entry)
    write(os.path.join(appdir, "usr", "share", "applications", PKG + ".desktop"), entry)
    icon = os.path.join(ROOT, "autorustdesk", "gui", "autorustdesk.png")
    copy(icon, os.path.join(appdir, PKG + ".png"))
    copy(icon, os.path.join(appdir, "usr", "share", "icons", "hicolor", "256x256", "apps", PKG + ".png"))
    os.symlink(PKG + ".png", os.path.join(appdir, ".DirIcon"))
    out = os.path.join(out_dir, "%s-%s-x86_64.AppImage" % (APP_NAME, __version__))
    env = dict(os.environ, ARCH="x86_64", APPIMAGE_EXTRACT_AND_RUN="1")
    run([appimagetool(work), "--no-appstream", appdir, out], env=env)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="制作 deb 和 AppImage")
    ap.add_argument("--bundle", default="", help="电脑 B 的离线包（.tar），一起打包进去")
    ap.add_argument("--client", default="", help="电脑 A 用的 RustDesk AppImage，一起打包进去")
    ap.add_argument("--no-appimage", action="store_true")
    ap.add_argument("--no-deb", action="store_true")
    args = ap.parse_args(argv)

    dist = os.path.join(ROOT, "dist")
    app_dir = os.path.join(dist, APP_NAME)
    if not os.path.isfile(os.path.join(app_dir, APP_NAME)):
        print("请先运行 packaging/build_linux.sh", file=sys.stderr)
        return 1
    work = os.path.join(ROOT, "build", "linux-packages")
    os.makedirs(work, exist_ok=True)
    add_extras(app_dir, args.bundle, args.client)
    outputs = []
    if not args.no_deb:
        outputs.append(build_deb(app_dir, dist, work))
    if not args.no_appimage:
        outputs.append(build_appimage(app_dir, dist, work))
    for o in outputs:
        print("完成：%s（%.0f MB）" % (o, os.path.getsize(o) / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
