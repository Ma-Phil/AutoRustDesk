"""打包 Windows / macOS 版（在对应的系统上运行）：

    pip install -r requirements.txt pyinstaller
    python packaging/build.py

输出：
    Windows：dist/AutoRustDesk-<版本>-windows-x64.zip   解压后运行 AutoRustDesk.exe
    macOS：  dist/AutoRustDesk-<版本>-macos-<arm64|x86_64>.zip   解压得到 AutoRustDesk.app

Linux 版请用 packaging/build_linux.sh。
"""

import os
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from autorustdesk import APP_NAME, __version__  # noqa: E402


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


def main() -> int:
    if sys.platform not in ("win32", "darwin"):
        print("Linux 版请用 packaging/build_linux.sh", file=sys.stderr)
        return 1
    dist = os.path.join(ROOT, "dist")
    build = os.path.join(ROOT, "build")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", dist,
         "--workpath", build, os.path.join(ROOT, "packaging", "AutoRustDesk.spec")])

    # 自检：打包后的程序能加载全部模块（界面、SSH、网卡接口）
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               AUTORUSTDESK_CONFIG_DIR=os.path.join(build, "selftest-config"))
    if sys.platform == "win32":
        folder = os.path.join(dist, APP_NAME)
        run([os.path.join(folder, APP_NAME + "-cli.exe"), "selftest"], env=env)
        run([os.path.join(folder, APP_NAME + "-cli.exe"), "bundle", "--help"], env=env,
            stdout=subprocess.DEVNULL)
        out = os.path.join(dist, "%s-%s-windows-x64.zip" % (APP_NAME, __version__))
        zip_dir(folder, out)
    else:
        app = os.path.join(dist, APP_NAME + ".app")
        run([os.path.join(app, "Contents", "MacOS", APP_NAME), "selftest"], env=env)
        arch = platform.machine() or "unknown"
        out = os.path.join(dist, "%s-%s-macos-%s.zip" % (APP_NAME, __version__, arch))
        if os.path.exists(out):
            os.remove(out)
        # ditto 保留 .app 里的符号链接和签名
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, out])
    shutil.rmtree(os.path.join(build, "selftest-config"), ignore_errors=True)
    print("完成：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
