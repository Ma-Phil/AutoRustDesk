"""离线部署包：格式定义与读取。

格式 2（当前）：一个 tar 文件（也可以是解开后的目录），可以同时包含多个 Ubuntu 版本的依赖：

    autorustdesk-bundle/
        manifest.json          元数据（RustDesk 版本、包含的 Ubuntu 版本、各版本的包清单）
        pool/*.deb             所有 .deb（RustDesk 本身和各版本的依赖，同名文件只存一份）
        repos/<代号>/          各版本本地 apt 源的索引（Packages、Packages.gz、Release）

上传到 B 之前，A 按 B 的版本取出一份"单版本包"（格式 1）：

    autorustdesk-bundle/
        manifest.json          只含这一个版本
        repo/                  扁平的本地 apt 源（*.deb + Packages + Release）

B 端用这个本地源离线安装 RustDesk 及其全部依赖。只含 20.04 的旧版（格式 1）离线包仍可使用。
"""

import copy
import io
import json
import os
import tarfile
from typing import Any, Dict, List, Optional

BUNDLE_FORMAT = 2
BUNDLE_ROOT = "autorustdesk-bundle"
MANIFEST_NAME = "manifest.json"
ARCH = "amd64"

# 支持的电脑 B 系统。xorg=False：GNOME 已不再提供 Xorg 会话（Ubuntu 26.04 起），
# RustDesk 无法控制登录界面，也无法在没接显示器时使用，只能部分支持。
RELEASES: Dict[str, Dict[str, Any]] = {
    "focal": {"version": "20.04", "xorg": True},
    "jammy": {"version": "22.04", "xorg": True},
    "noble": {"version": "24.04", "xorg": True},
    "resolute": {"version": "26.04", "xorg": False},
}
DEFAULT_RELEASES = ["focal", "jammy", "noble"]

# 除 RustDesk 以外需要在 B 上安装的包：无显示器时使用的虚拟显示驱动
EXTRA_PACKAGES = ["xserver-xorg-video-dummy"]


def release_label(codename: str) -> str:
    info = RELEASES.get(codename)
    return "Ubuntu %s" % info["version"] if info else codename


def parse_releases(text: str) -> List[str]:
    """把 "20.04,22.04" 或 "focal,jammy" 解析成代号列表。"""
    by_version = {v["version"]: k for k, v in RELEASES.items()}
    result: List[str] = []
    for item in (text or "").replace("，", ",").split(","):
        item = item.strip().lower()
        if not item:
            continue
        code = by_version.get(item, item)
        if code not in RELEASES:
            raise BundleError("不支持的 Ubuntu 版本：%s（可选：%s）" % (
                item, "、".join("%s(%s)" % (v["version"], k) for k, v in RELEASES.items())))
        if code not in result:
            result.append(code)
    return result


class BundleError(Exception):
    pass


class Bundle:
    """已制作好的离线部署包（tar 文件或目录）。"""

    def __init__(self, path: str, manifest: Dict[str, Any], root: str = ""):
        self.path = path
        self.manifest = manifest
        self.root = root  # 目录形式时 manifest.json 所在目录

    @classmethod
    def open(cls, path: str) -> "Bundle":
        path = os.path.abspath(path)
        if os.path.isdir(path):
            for root in (path, os.path.join(path, BUNDLE_ROOT)):
                m = os.path.join(root, MANIFEST_NAME)
                if os.path.isfile(m):
                    with open(m, "r", encoding="utf-8") as f:
                        return cls(path, cls._validate(json.load(f)), root)
            raise BundleError("目录中没有 %s：%s" % (MANIFEST_NAME, path))
        if not os.path.isfile(path):
            raise BundleError("找不到离线包：%s" % path)
        try:
            with tarfile.open(path, "r:*") as tf:
                member = tf.getmember("%s/%s" % (BUNDLE_ROOT, MANIFEST_NAME))
                f = tf.extractfile(member)
                assert f is not None
                return cls(path, cls._validate(json.loads(f.read().decode("utf-8"))))
        except (tarfile.TarError, KeyError) as e:
            raise BundleError("不是有效的离线包：%s（%s）" % (path, e))

    @staticmethod
    def _validate(manifest: Dict[str, Any]) -> Dict[str, Any]:
        fmt = manifest.get("format")
        if fmt == 1:
            required = ("rustdesk", "target", "install_packages", "packages")
        elif fmt == 2:
            required = ("rustdesk", "releases", "install_packages")
        else:
            raise BundleError("离线包格式版本不受支持：%r（请用新版本重新制作）" % fmt)
        for key in required:
            if key not in manifest:
                raise BundleError("离线包 manifest 缺少字段：%s" % key)
        return manifest

    # ------------------------------------------------------------------ 信息

    @property
    def is_dir(self) -> bool:
        return os.path.isdir(self.path)

    @property
    def format(self) -> int:
        return int(self.manifest["format"])

    @property
    def rustdesk_version(self) -> str:
        return self.manifest["rustdesk"]["version"]

    @property
    def install_packages(self) -> List[str]:
        return list(self.manifest["install_packages"])

    @property
    def releases(self) -> Dict[str, Dict[str, Any]]:
        """{代号: {"version": ..., "packages": [...]}}"""
        if self.format == 1:
            t = self.manifest["target"]
            return {t["codename"]: {"version": t.get("release", ""),
                                    "packages": self.manifest["packages"]}}
        return self.manifest["releases"]

    def has_release(self, codename: str) -> bool:
        return codename in self.releases

    @property
    def size(self) -> int:
        if self.is_dir:
            total = 0
            for root, _dirs, files in os.walk(self.path):
                for name in files:
                    total += os.path.getsize(os.path.join(root, name))
            return total
        return os.path.getsize(self.path)

    def summary(self) -> str:
        labels = " / ".join(release_label(c) for c in self.releases)
        return "RustDesk %s · %s · x86_64 · %.1f MB" % (
            self.rustdesk_version, labels, self.size / 1024.0 / 1024.0)

    # ------------------------------------------------------------------ 单版本包

    def release_manifest(self, codename: str) -> Dict[str, Any]:
        """给 B 用的单版本（格式 1）manifest。"""
        if not self.has_release(codename):
            raise BundleError("离线包里没有 %s 的依赖" % release_label(codename))
        if self.format == 1:
            return copy.deepcopy(self.manifest)
        m = self.manifest
        rel = m["releases"][codename]
        rustdesk = dict(m["rustdesk"])
        rustdesk["file"] = "repo/" + os.path.basename(rustdesk["file"])
        return {
            "format": 1,
            "created": m.get("created"),
            "builder": m.get("builder"),
            "target": {"distro": "ubuntu", "release": rel.get("version", ""),
                       "codename": codename, "arch": ARCH},
            "mirror": m.get("mirror"),
            "rustdesk": rustdesk,
            "install_packages": list(m["install_packages"]),
            "packages": rel["packages"],
        }

    def write_release_tar(self, codename: str, dest: str) -> str:
        """取出 B 需要的那一个版本，写成单版本 tar（格式 1）。返回写出的路径。"""
        manifest = self.release_manifest(codename)
        with tarfile.open(dest, "w") as out:
            _add_bytes(out, "%s/%s" % (BUNDLE_ROOT, MANIFEST_NAME),
                       json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            if self.format == 1:
                self._copy_v1_repo(out)
            else:
                self._copy_v2_release(out, codename, manifest)
        return dest

    def _copy_v1_repo(self, out: tarfile.TarFile) -> None:
        prefix = "%s/repo/" % BUNDLE_ROOT
        if self.is_dir:
            repo = os.path.join(self.root, "repo")
            for name in sorted(os.listdir(repo)):
                out.add(os.path.join(repo, name), arcname=prefix + name, filter=_normalize)
            return
        with tarfile.open(self.path, "r:*") as src:
            for m in src.getmembers():
                if m.isfile() and m.name.startswith(prefix):
                    out.addfile(_normalize(m), src.extractfile(m))

    def _copy_v2_release(self, out: tarfile.TarFile, codename: str, manifest: Dict[str, Any]) -> None:
        index_names = ["Packages", "Packages.gz", "Release"]
        debs = [os.path.basename(p["file"]) for p in manifest["packages"]]
        debs.append(os.path.basename(self.manifest["rustdesk"]["file"]))
        wanted = {"repos/%s/%s" % (codename, n): "repo/" + n for n in index_names}
        wanted.update({"pool/" + d: "repo/" + d for d in debs})
        if self.is_dir:
            for src_rel, dst_rel in sorted(wanted.items()):
                src = os.path.join(self.root, src_rel)
                if not os.path.isfile(src):
                    raise BundleError("离线包不完整，缺少 %s" % src_rel)
                out.add(src, arcname="%s/%s" % (BUNDLE_ROOT, dst_rel), filter=_normalize)
            return
        found = set()
        prefix = BUNDLE_ROOT + "/"
        with tarfile.open(self.path, "r:*") as src:
            for m in src:
                rel = m.name[len(prefix):] if m.name.startswith(prefix) else ""
                if m.isfile() and rel in wanted:
                    info = copy.copy(m)
                    info.name = prefix + wanted[rel]
                    out.addfile(_normalize(info), src.extractfile(m))
                    found.add(rel)
        missing = set(wanted) - found
        if missing:
            raise BundleError("离线包不完整，缺少 %s" % "、".join(sorted(missing)[:5]))


def _normalize(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    ti.mode = 0o755 if ti.isdir() else 0o644
    return ti


def _add_bytes(tf: tarfile.TarFile, name: str, data: bytes) -> None:
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    tf.addfile(_normalize(ti), io.BytesIO(data))


def rustdesk_file_name(manifest: Dict[str, Any]) -> Optional[str]:
    f = manifest.get("rustdesk", {}).get("file")
    return os.path.basename(f) if f else None
