"""下载 RustDesk 官方安装包（GitHub Releases），用于制作完整离线版。

    python packaging/fetch_rustdesk.py latest deb 输出目录            # 电脑 B 用的 deb
    python packaging/fetch_rustdesk.py 1.4.2 windows 输出目录         # 电脑 A（Windows）用的 exe
    python packaging/fetch_rustdesk.py latest macos-arm64 输出目录     # Apple 芯片 Mac 的 dmg
    python packaging/fetch_rustdesk.py latest appimage 输出目录        # 电脑 A（Linux）用的 AppImage

latest 表示最新的正式版。打印实际下载的版本；加 --github-output 时同时写入 GitHub Actions 的输出。
有 GITHUB_TOKEN 环境变量时带上它，避免 GitHub API 的匿名访问次数限制。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request

REPO = "rustdesk/rustdesk"
KINDS = {
    "deb": r"rustdesk-{v}-x86_64\.deb",
    "windows": r"rustdesk-{v}-x86_64\.exe",
    "macos-arm64": r"rustdesk-{v}-aarch64\.dmg",
    "appimage": r"rustdesk-{v}-x86_64\.AppImage",
}


def _get(url: str, accept: str = "application/vnd.github+json") -> urllib.request.addinfourl:
    headers = {"Accept": accept, "User-Agent": "AutoRustDesk-build"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = "Bearer " + token
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)


def release_info(version: str) -> dict:
    path = "latest" if version == "latest" else "tags/" + version
    with _get("https://api.github.com/repos/%s/releases/%s" % (REPO, path)) as r:
        return json.load(r)


def download(url: str, dest: str) -> str:
    h = hashlib.sha256()
    tmp = dest + ".part"
    with _get(url, accept="application/octet-stream") as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
    os.replace(tmp, dest)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("version", help="RustDesk 版本，例如 1.4.2；latest 表示最新正式版")
    ap.add_argument("kind", choices=sorted(KINDS))
    ap.add_argument("outdir")
    ap.add_argument("--github-output", action="store_true", help="把版本号写入 GITHUB_OUTPUT")
    args = ap.parse_args(argv)

    rel = release_info(args.version)
    version = rel["tag_name"].lstrip("v")
    pattern = re.compile("^" + KINDS[args.kind].format(v=re.escape(version)) + "$")
    assets = rel.get("assets", [])
    asset = next((a for a in assets if pattern.match(a["name"])), None)
    if asset is None:
        print("RustDesk %s 里没有找到 %s，发布页上的文件有：\n  %s" % (
            version, args.kind, "\n  ".join(a["name"] for a in assets)), file=sys.stderr)
        return 1
    os.makedirs(args.outdir, exist_ok=True)
    dest = os.path.join(args.outdir, asset["name"])
    print("下载 %s（%.1f MB）" % (asset["name"], asset.get("size", 0) / 1048576.0), flush=True)
    sha = download(asset["browser_download_url"], dest)
    expected = (asset.get("digest") or "").partition("sha256:")[2]
    if expected and expected != sha:
        os.remove(dest)
        print("校验失败：%s 的 SHA256 与发布页不符" % asset["name"], file=sys.stderr)
        return 1
    print("RustDesk %s → %s（SHA256 %s）" % (version, dest, sha))
    if args.github_output and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("version=%s\nfile=%s\n" % (version, dest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
