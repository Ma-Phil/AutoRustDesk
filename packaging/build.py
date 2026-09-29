"""打包 Windows / macOS 版（在对应的系统上运行）：

    pip install --prefer-binary -r requirements.txt pyinstaller
    python packaging/build.py [--bundle 离线包.tar] [--client RustDesk 安装包]

输出：
    Windows：dist/AutoRustDesk-<版本>-windows-x64-setup.exe（需要 Inno Setup 6；没有时输出 zip）
    macOS：  dist/AutoRustDesk-<版本>-macos-arm64.dmg（发布的只有 Apple 芯片版）

--bundle：电脑 B 的离线部署包，打包进程序里（程序自动使用）。
--client：电脑 A 用的 RustDesk 官方安装包：Windows 为 rustdesk-<版本>-x86_64.exe，放进程序目录；
          macOS 为 rustdesk-<版本>-aarch64.dmg，取出其中的 RustDesk.app 放进安装盘。
两者都给出时就是完整离线版，电脑 A 全程不用联网。可以用 packaging/fetch_rustdesk.py 下载官方安装包。

--prefer-binary：只用预编译好的包。在打包机上从源码编译的版本（例如 Intel Mac 上的 cryptography）
可能和打包进去的库冲突，打包后无法加载（打包后的自检会报错）。

Linux 版请用 packaging/build_linux.sh 和 packaging/linux_packages.py。
"""

import argparse
import glob
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from autorustdesk import APP_NAME, __version__  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

NOTICES = os.path.join(ROOT, "packaging", "THIRD_PARTY_NOTICES.txt")


def run(cmd, **kw):
    print("+", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, **kw)


def zip_dir(src: str, out: str) -> None:
    base = os.path.dirname(src)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(src):
            for name in files:
                path = os.path.join(root, name)
                z.write(path, os.path.relpath(path, base))


def selftest(exe: str, build: str) -> dict:
    """打包后的程序能加载全部模块（界面、SSH、网卡接口），并报告内置资源。"""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               AUTORUSTDESK_CONFIG_DIR=os.path.join(build, "selftest-config"))
    env.pop("AUTORUSTDESK_RESOURCES", None)
    print("+", exe, "selftest", flush=True)
    out = subprocess.run([exe, "selftest"], env=env, stdout=subprocess.PIPE, check=True).stdout
    text = out.decode("utf-8", "replace")
    print(text, flush=True)
    return json.loads(text[text.index("{"):])


def check_builtin(info: dict, bundle: str, client: str) -> None:
    """给了离线包 / RustDesk 客户端时，打包后的程序必须能找到它们。"""
    if bundle and not info.get("builtin_bundle"):
        raise SystemExit("打包后的程序没有找到自带的离线包")
    if client and not info.get("builtin_client"):
        raise SystemExit("打包后的程序没有找到自带的 RustDesk 客户端")


def find_iscc() -> str:
    # 先找安装目录（旁边的 Languages 里有翻译），再找 PATH（可能是 Chocolatey 的转发程序）
    bases = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
             r"C:\Program Files (x86)", r"C:\Program Files"]
    for base in bases:
        if base:
            path = os.path.join(base, "Inno Setup 6", "ISCC.exe")
            if os.path.isfile(path):
                return path
    return shutil.which("ISCC") or shutil.which("iscc") or ""


def chinese_messages(iscc: str) -> str:
    """安装向导的简体中文文字：Inno Setup 带了就用它的，没带时用我们自己的（只翻译了常见界面）。"""
    langs = sorted(os.path.basename(p)[:-4]
                   for p in glob.glob(os.path.join(os.path.dirname(iscc), "Languages", "*.isl")))
    print("Inno Setup 带的翻译：%s" % ("、".join(langs) or "没有找到"), flush=True)
    found = os.path.join(os.path.dirname(iscc), "Languages", "ChineseSimplified.isl")
    if not os.path.isfile(found):
        found = os.path.join(ROOT, "packaging", "windows", "ChineseSimplified.isl")
    print("安装向导的中文：%s" % found, flush=True)
    return found


def verify_installer(setup: str, build: str, bundle: str, client: str) -> None:
    """静默安装到临时目录，运行装好的程序自检，再静默卸载。"""
    target = os.path.join(build, "install-test")
    shutil.rmtree(target, ignore_errors=True)
    run([setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
         "/DIR=%s" % target, "/LOG=%s" % os.path.join(build, "install-test.log")])
    for name in (APP_NAME + ".exe", "unins000.exe"):
        if not os.path.isfile(os.path.join(target, name)):
            raise SystemExit("安装后没有 %s" % name)
    check_builtin(selftest(os.path.join(target, APP_NAME + "-cli.exe"), build), bundle, client)
    run([os.path.join(target, "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])


def build_windows(dist: str, build: str, bundle: str, client: str) -> str:
    folder = os.path.join(dist, APP_NAME)
    cli = os.path.join(folder, APP_NAME + "-cli.exe")
    # 离线包和 RustDesk 客户端放在程序目录里，程序自动使用
    for sub, src in (("bundle", bundle), ("rustdesk", client)):
        if src:
            os.makedirs(os.path.join(folder, sub), exist_ok=True)
            shutil.copy2(src, os.path.join(folder, sub, os.path.basename(src)))
    shutil.copy2(NOTICES, folder)
    check_builtin(selftest(cli, build), bundle, client)
    run([cli, "bundle", "--help"], stdout=subprocess.DEVNULL)
    iscc = find_iscc()
    if iscc:
        run([iscc, "/Q", "/DAppVersion=%s" % __version__, "/DSourceDir=%s" % folder,
             "/DOutputDir=%s" % dist,
             "/DIconFile=%s" % os.path.join(ROOT, "packaging", "icons", "autorustdesk.ico"),
             "/DChsFile=%s" % chinese_messages(iscc),
             os.path.join(ROOT, "packaging", "windows", "AutoRustDesk.iss")])
        setup = os.path.join(dist, "%s-%s-windows-x64-setup.exe" % (APP_NAME, __version__))
        verify_installer(setup, build, bundle, client)
        return setup
    print("没有找到 Inno Setup，改为输出 zip（解压后运行 AutoRustDesk.exe）")
    out = os.path.join(dist, "%s-%s-windows-x64.zip" % (APP_NAME, __version__))
    zip_dir(folder, out)
    return out


def copy_rustdesk_app(dmg: str, dest_dir: str) -> None:
    """从 RustDesk 官方 dmg 里取出 RustDesk.app（保留原签名）。"""
    mnt = tempfile.mkdtemp(prefix="rustdesk-dmg-")
    cmd = ["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mnt, dmg]
    print("+", " ".join(cmd), flush=True)
    # 安装盘带许可协议时 hdiutil 会等着输入同意，替它回答
    subprocess.run(cmd, input=b"Y\n", stdout=subprocess.DEVNULL, check=True)
    try:
        apps = glob.glob(os.path.join(mnt, "*.app"))
        if not apps:
            raise SystemExit("RustDesk 的安装盘里没有 .app：%s" % dmg)
        run(["ditto", apps[0], os.path.join(dest_dir, os.path.basename(apps[0]))])
    finally:
        run(["hdiutil", "detach", mnt, "-force"], stdout=subprocess.DEVNULL)


def verify_dmg(dmg: str, build: str, bundle: str, client: str) -> None:
    """挂载做好的安装盘，从里面运行本程序自检：要能找到自带的离线包和旁边的 RustDesk.app，
    RustDesk.app 的官方签名要完好（否则用户打不开）。"""
    mnt = tempfile.mkdtemp(prefix="autorustdesk-dmg-")
    run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mnt, dmg],
        stdout=subprocess.DEVNULL)
    try:
        info = selftest(os.path.join(mnt, APP_NAME + ".app", "Contents", "MacOS", APP_NAME), build)
        check_builtin(info, bundle, client)
        if client:
            if info.get("builtin_client") != "RustDesk":
                raise SystemExit("安装盘里的程序没有找到旁边的 RustDesk.app")
            run(["codesign", "--verify", "--deep", "--strict", "--verbose=1",
                 os.path.join(mnt, "RustDesk.app")])
    finally:
        run(["hdiutil", "detach", mnt, "-force"], stdout=subprocess.DEVNULL)


def build_macos(dist: str, build: str, bundle: str, client: str) -> str:
    app = os.path.join(dist, APP_NAME + ".app")
    with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
        print("最低 macOS 版本：%s" % plistlib.load(f).get("LSMinimumSystemVersion"), flush=True)
    check_builtin(selftest(os.path.join(app, "Contents", "MacOS", APP_NAME), build), bundle, "")
    # 安装盘：本程序 + RustDesk（可选）+"应用程序"快捷方式，两个都拖进去就装好了
    stage = os.path.join(build, "dmg")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    run(["ditto", app, os.path.join(stage, os.path.basename(app))])
    if client:
        copy_rustdesk_app(client, stage)
    os.symlink("/Applications", os.path.join(stage, "Applications"))
    shutil.copy2(NOTICES, stage)
    arch = platform.machine() or "unknown"
    out = os.path.join(dist, "%s-%s-macos-%s.dmg" % (APP_NAME, __version__, arch))
    if os.path.exists(out):
        os.remove(out)
    cmd = ["hdiutil", "create", "-volname", "%s %s" % (APP_NAME, __version__), "-srcfolder", stage,
           "-fs", "HFS+", "-format", "UDZO", "-ov", out]
    # GitHub 的 macOS 机器上 hdiutil 偶尔报"资源忙"（系统在扫描新文件），等一下重试
    for attempt in range(3):
        try:
            run(cmd)
            break
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(15)
    verify_dmg(out, build, bundle, client)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打包 Windows / macOS 版")
    ap.add_argument("--bundle", default="", help="电脑 B 的离线部署包（.tar），打包进程序")
    ap.add_argument("--client", default="", help="电脑 A 用的 RustDesk 官方安装包（Windows exe / macOS dmg）")
    args = ap.parse_args(argv)
    if sys.platform not in ("win32", "darwin"):
        print("Linux 版请用 packaging/build_linux.sh 和 packaging/linux_packages.py", file=sys.stderr)
        return 1
    for path in (args.bundle, args.client):
        if path and not os.path.isfile(path):
            print("文件不存在：%s" % path, file=sys.stderr)
            return 1
    dist = os.path.join(ROOT, "dist")
    build = os.path.join(ROOT, "build")
    env = dict(os.environ)
    if args.bundle and sys.platform == "darwin":
        # macOS 的离线包要放进 .app 里面（签名之前），由 AutoRustDesk.spec 读取这个变量
        env["AUTORUSTDESK_BUNDLE"] = os.path.abspath(args.bundle)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", dist,
         "--workpath", build, os.path.join(ROOT, "packaging", "AutoRustDesk.spec")], env=env)
    if sys.platform == "win32":
        out = build_windows(dist, build, args.bundle, args.client)
    else:
        out = build_macos(dist, build, args.bundle, args.client)
    shutil.rmtree(os.path.join(build, "selftest-config"), ignore_errors=True)
    print("完成：%s（%.0f MB）" % (out, os.path.getsize(out) / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
