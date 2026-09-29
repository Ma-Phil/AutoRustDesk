"""离线部署包：格式定义与读取。

部署包是一个 tar 文件（也可以是解开后的目录），结构：

    autorustdesk-bundle/
        manifest.json      元数据（RustDesk 版本、目标系统、包清单）
        repo/              扁平的本地 apt 软件源（*.deb + Packages + Release）

B 端用这个本地源离线安装 RustDesk 及其全部依赖。
"""

import json
import os
import tarfile
from typing import Any, Dict, List, Optional

BUNDLE_FORMAT = 1
BUNDLE_ROOT = "autorustdesk-bundle"
MANIFEST_NAME = "manifest.json"

# 部署包的目标系统（电脑 B）
TARGET = {"distro": "ubuntu", "release": "20.04", "codename": "focal", "arch": "amd64"}

# 除 RustDesk 以外需要在 B 上安装的包：无显示器时使用的虚拟显示驱动
EXTRA_PACKAGES = ["xserver-xorg-video-dummy"]


class BundleError(Exception):
    pass


class Bundle:
    """已制作好的离线部署包（tar 文件或目录）。"""

    def __init__(self, path: str, manifest: Dict[str, Any]):
        self.path = path
        self.manifest = manifest

    @classmethod
    def open(cls, path: str) -> "Bundle":
        path = os.path.abspath(path)
        if os.path.isdir(path):
            candidates = [
                os.path.join(path, MANIFEST_NAME),
                os.path.join(path, BUNDLE_ROOT, MANIFEST_NAME),
            ]
            for c in candidates:
                if os.path.isfile(c):
                    with open(c, "r", encoding="utf-8") as f:
                        return cls(path, cls._validate(json.load(f)))
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
        if manifest.get("format") != BUNDLE_FORMAT:
            raise BundleError("离线包格式版本不受支持：%r" % manifest.get("format"))
        for key in ("rustdesk", "target", "install_packages", "packages"):
            if key not in manifest:
                raise BundleError("离线包 manifest 缺少字段：%s" % key)
        return manifest

    @property
    def is_dir(self) -> bool:
        return os.path.isdir(self.path)

    @property
    def rustdesk_version(self) -> str:
        return self.manifest["rustdesk"]["version"]

    @property
    def install_packages(self) -> List[str]:
        return list(self.manifest["install_packages"])

    @property
    def target(self) -> Dict[str, str]:
        return self.manifest["target"]

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
        t = self.target
        return "RustDesk %s · %s %s %s · %d 个软件包 · %.1f MB" % (
            self.rustdesk_version,
            t.get("distro", "?"),
            t.get("release", "?"),
            t.get("arch", "?"),
            len(self.manifest["packages"]),
            self.size / 1024.0 / 1024.0,
        )

    def rustdesk_deb_name(self) -> Optional[str]:
        return self.manifest["rustdesk"].get("file")
