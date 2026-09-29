"""制作离线部署包（需要联网）。

不依赖 apt / Docker：直接读取所选 Ubuntu 版本（20.04 / 22.04 / 24.04 / 26.04）
软件源的 Packages 索引，分别计算 RustDesk 及额外软件包的完整依赖闭包，
下载并校验每个 .deb（各版本共用、同名文件只存一份），为每个版本生成本地
软件源索引和 manifest.json，最后打成一个 tar。

包含完整闭包（连 libc6 等基础包也带上）是有意为之：B 上 apt 只会
安装缺少的包、必要时升级，多带的包不会被装上。
"""

import concurrent.futures
import gzip
import hashlib
import json
import lzma
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from email.utils import formatdate
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .. import __version__
from ..debian import (
    Relation,
    Stanza,
    iter_stanzas,
    parse_relations,
    read_deb_control,
    version_compare,
    version_satisfies,
)
from . import (ARCH, BUNDLE_FORMAT, BUNDLE_ROOT, DEFAULT_RELEASES, EXTRA_PACKAGES, MANIFEST_NAME,
               RELEASES, release_label)

MIRRORS = [
    ("Ubuntu 官方源", "http://archive.ubuntu.com/ubuntu"),
    ("清华大学 TUNA（国内推荐）", "https://mirrors.tuna.tsinghua.edu.cn/ubuntu"),
    ("阿里云", "https://mirrors.aliyun.com/ubuntu"),
    ("中国科学技术大学", "https://mirrors.ustc.edu.cn/ubuntu"),
]
DEFAULT_MIRROR = MIRRORS[0][1]



def suites_for(codename: str) -> List[str]:
    return [codename, codename + "-updates", codename + "-security"]


COMPONENTS = ["main", "restricted", "universe", "multiverse"]
ARCHES = ("amd64", "all")
UBUNTU_KEYRING = "/usr/share/keyrings/ubuntu-archive-keyring.gpg"

LogFn = Callable[[str], None]
ProgressFn = Callable[[str, float], None]


class BuildError(Exception):
    pass


def _noop_log(msg: str) -> None:
    pass


def _noop_progress(stage: str, fraction: float) -> None:
    pass


def default_cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "autorustdesk")


def _http_get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "AutoRustDesk/%s" % __version__})
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001 - 网络错误统一重试
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise BuildError("下载失败：%s（%s）" % (url, last_err))


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hashes(path: str) -> Tuple[str, str, int]:
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)
            sha.update(chunk)
            size += len(chunk)
    return md5.hexdigest(), sha.hexdigest(), size


# ---------------------------------------------------------------------------
# 软件源索引
# ---------------------------------------------------------------------------


def parse_release_hashes(text: str) -> Dict[str, Tuple[str, int]]:
    """从 (In)Release 中取出 SHA256 段：{相对路径: (sha256, size)}。"""
    result: Dict[str, Tuple[str, int]] = {}
    in_sha = False
    for line in text.splitlines():
        if line.startswith("SHA256:"):
            in_sha = True
            continue
        if in_sha:
            if not line.startswith(" "):
                in_sha = False
                continue
            parts = line.split()
            if len(parts) == 3:
                result[parts[2]] = (parts[0], int(parts[1]))
    return result


class PackageIndex:
    """合并多个 suite/component 的 Packages 索引。"""

    def __init__(self) -> None:
        self.by_name: Dict[str, List[Stanza]] = {}
        self.providers: Dict[str, List[Tuple[Stanza, Optional[str]]]] = {}

    def add_text(self, text: str) -> int:
        count = 0
        for st in iter_stanzas(text):
            if "Package" not in st or "Version" not in st:
                continue
            if st.get("Architecture", "all") not in ARCHES:
                continue
            self.add(st)
            count += 1
        return count

    def add(self, st: Stanza) -> None:
        versions = self.by_name.setdefault(st.name, [])
        if any(v.version == st.version for v in versions):
            return
        versions.append(st)
        versions.sort(key=_VersionKey, reverse=True)
        for group in parse_relations(st.get("Provides", "")):
            for rel in group:
                self.providers.setdefault(rel.name, []).append(
                    (st, rel.version if rel.op == "=" else None)
                )

    def candidates(self, rel: Relation) -> List[Stanza]:
        """满足关系的真实包（高版本在前）。"""
        return [
            st for st in self.by_name.get(rel.name, [])
            if version_satisfies(st.version, rel.op, rel.version)
        ]

    def provided_by(self, rel: Relation) -> List[Stanza]:
        """通过 Provides 满足关系的包。"""
        out = []
        for st, pver in self.providers.get(rel.name, []):
            if rel.op:
                if pver is None or not version_satisfies(pver, rel.op, rel.version):
                    continue
            out.append(st)
        out.sort(key=_VersionKey, reverse=True)
        return out

    def __len__(self) -> int:
        return sum(len(v) for v in self.by_name.values())


class _VersionKey:
    """让 list.sort 按 Debian 版本排序。"""

    def __init__(self, st: Stanza):
        self.v = st.version

    def __lt__(self, other: "_VersionKey") -> bool:
        return version_compare(self.v, other.v) < 0


def load_index(
    mirror: str,
    cache_dir: str,
    suites: Iterable[str],
    components: Iterable[str] = COMPONENTS,
    log: LogFn = _noop_log,
    verify_gpg: str = "auto",
) -> PackageIndex:
    """下载（或复用缓存）并解析软件源索引。verify_gpg: auto / yes / no。"""
    mirror = mirror.rstrip("/")
    index = PackageIndex()
    mkey = hashlib.sha256(mirror.encode()).hexdigest()[:12]
    for suite in suites:
        base = "%s/dists/%s" % (mirror, suite)
        log("读取软件源 %s/%s ..." % (mirror, suite))
        inrelease = _http_get(base + "/InRelease")
        _verify_inrelease(inrelease, suite, log, verify_gpg)
        hashes = parse_release_hashes(inrelease.decode("utf-8", "replace"))
        for comp in components:
            rel_path = "%s/binary-%s/Packages.xz" % (comp, ARCH)
            if rel_path not in hashes:
                continue
            want_sha, want_size = hashes[rel_path]
            cache_path = os.path.join(cache_dir, "indices", mkey, suite, rel_path)
            if not (os.path.isfile(cache_path) and _sha256_file(cache_path) == want_sha):
                blob = _http_get("%s/%s" % (base, rel_path), timeout=300)
                if hashlib.sha256(blob).hexdigest() != want_sha or len(blob) != want_size:
                    raise BuildError("索引校验失败：%s/%s" % (base, rel_path))
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path + ".tmp", "wb") as f:
                    f.write(blob)
                os.replace(cache_path + ".tmp", cache_path)
            with open(cache_path, "rb") as f:
                text = lzma.decompress(f.read()).decode("utf-8", "replace")
            n = index.add_text(text)
            log("  %s/%s：%d 个包" % (suite, comp, n))
    return index


def _verify_inrelease(data: bytes, suite: str, log: LogFn, mode: str) -> None:
    if mode == "no":
        return
    gpgv = shutil.which("gpgv")
    if not gpgv or not os.path.isfile(UBUNTU_KEYRING):
        if mode == "yes":
            raise BuildError("无法校验软件源签名：缺少 gpgv 或 %s" % UBUNTU_KEYRING)
        log("  提示：本机没有 gpgv/Ubuntu 密钥环，跳过 %s 的签名校验（仍会校验每个文件的 SHA256）" % suite)
        return
    with tempfile.NamedTemporaryFile(suffix=".InRelease", delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        r = subprocess.run(
            [gpgv, "--keyring", UBUNTU_KEYRING, tmp],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if r.returncode != 0:
            raise BuildError(
                "软件源 %s 的签名校验失败：\n%s" % (suite, r.stdout.decode("utf-8", "replace"))
            )
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# 依赖闭包
# ---------------------------------------------------------------------------


_PRIORITY_RANK = {"required": 0, "important": 1, "standard": 2, "optional": 3, "extra": 4}


class Resolver:
    def __init__(self, index: PackageIndex):
        self.index = index
        self.selected: Dict[str, List[Stanza]] = {}
        # 已选中包通过 Provides 提供的虚拟包：{虚拟包名: [提供的版本或 None]}
        self.provided: Dict[str, List[Optional[str]]] = {}
        self.why: Dict[str, str] = {}

    def _selected_satisfies(self, rel: Relation) -> bool:
        for st in self.selected.get(rel.name, []):
            if version_satisfies(st.version, rel.op, rel.version):
                return True
        for pver in self.provided.get(rel.name, []):
            if not rel.op:
                return True
            if pver is not None and version_satisfies(pver, rel.op, rel.version):
                return True
        return False

    def _choose(self, rel: Relation) -> Optional[Stanza]:
        cands = self.index.candidates(rel)
        if cands:
            return cands[0]
        provs = self.index.provided_by(rel)
        if provs:
            # 多个提供者时：优先已选中的，其次优先级高的（与 apt 的倾向一致），最后按名字
            for st in provs:
                if st.name in self.selected:
                    return st
            provs.sort(key=lambda s: (_PRIORITY_RANK.get(s.get("Priority", ""), 5), s.name))
            return provs[0]
        return None

    def _select(self, st: Stanza) -> None:
        self.selected.setdefault(st.name, []).append(st)
        for group in st.relations("Provides"):
            for p in group:
                self.provided.setdefault(p.name, []).append(p.version if p.op == "=" else None)

    def add_root(self, group: List[Relation], reason: str) -> None:
        self._resolve_group(group, reason)

    def _resolve_group(self, group: List[Relation], reason: str) -> None:
        pending = [(group, reason)]
        while pending:
            grp, why = pending.pop()
            if any(self._selected_satisfies(r) for r in grp):
                continue
            chosen = None
            for rel in grp:
                chosen = self._choose(rel)
                if chosen is not None:
                    break
            if chosen is None:
                raise BuildError(
                    "无法满足依赖：%s（来自 %s）" % (" | ".join(str(r) for r in grp), why)
                )
            if any(v.version == chosen.version for v in self.selected.get(chosen.name, [])):
                continue
            self._select(chosen)
            self.why.setdefault(chosen.name, why)
            for field in ("Pre-Depends", "Depends"):
                for sub in chosen.relations(field):
                    pending.append((sub, chosen.name))

    def result(self) -> List[Stanza]:
        out = []
        for name in sorted(self.selected):
            out.extend(self.selected[name])
        return out


def resolve_closure(
    index: PackageIndex, root_groups: List[Tuple[List[Relation], str]]
) -> List[Stanza]:
    resolver = Resolver(index)
    for group, reason in root_groups:
        resolver.add_root(group, reason)
    return resolver.result()


# ---------------------------------------------------------------------------
# 下载与本地软件源
# ---------------------------------------------------------------------------


def _download_debs(
    mirror: str,
    stanzas: List[Stanza],
    dest_dir: str,
    cache_dir: str,
    log: LogFn,
    progress: ProgressFn,
    workers: int = 6,
) -> None:
    mirror = mirror.rstrip("/")
    pool_cache = os.path.join(cache_dir, "debs")
    os.makedirs(pool_cache, exist_ok=True)
    total = sum(int(st.get("Size", "0")) for st in stanzas) or 1
    done = [0]

    def fetch(st: Stanza) -> None:
        filename = st["Filename"]
        want_sha = st.get("SHA256", "")
        base = os.path.basename(filename)
        dest = os.path.join(dest_dir, base)
        if os.path.isfile(dest):
            # 另一个 Ubuntu 版本已经放进来了同一个文件
            if _sha256_file(dest) != want_sha:
                raise BuildError("不同版本里的同名文件内容不一致：%s" % base)
            done[0] += int(st.get("Size", "0"))
            return
        cached = os.path.join(pool_cache, base)
        if not (os.path.isfile(cached) and _sha256_file(cached) == want_sha):
            blob = _http_get("%s/%s" % (mirror, filename), timeout=600)
            if hashlib.sha256(blob).hexdigest() != want_sha:
                raise BuildError("文件校验失败：%s" % filename)
            with open(cached + ".tmp", "wb") as f:
                f.write(blob)
            os.replace(cached + ".tmp", cached)
        shutil.copyfile(cached, dest)
        done[0] += int(st.get("Size", "0"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch, st): st for st in stanzas}
        for n, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            fut.result()
            progress("下载软件包", done[0] / total)
            if n % 20 == 0 or n == len(futures):
                log("  已下载 %d/%d 个软件包（%.1f MB）" % (n, len(futures), done[0] / 1048576.0))


_FILENAME_RE = re.compile(r"^Filename:.*$", re.M)


def _local_stanza_text(st: Stanza) -> str:
    base = os.path.basename(st["Filename"])
    return _FILENAME_RE.sub("Filename: ./%s" % base, st.raw)


def write_local_repo(repo_dir: str, stanza_texts: List[str]) -> None:
    """写扁平本地源的 Packages / Packages.gz / Release。"""
    packages = "\n\n".join(t.strip() for t in stanza_texts) + "\n"
    p_path = os.path.join(repo_dir, "Packages")
    with open(p_path, "w", encoding="utf-8") as f:
        f.write(packages)
    with open(os.path.join(repo_dir, "Packages.gz"), "wb") as f:
        f.write(gzip.compress(packages.encode("utf-8"), mtime=0))
    lines = [
        "Origin: AutoRustDesk",
        "Label: AutoRustDesk offline bundle",
        "Suite: autorustdesk",
        "Codename: autorustdesk",
        "Date: %s" % formatdate(usegmt=True),
        "Architectures: %s" % " ".join(ARCHES),
        "Description: AutoRustDesk offline repository",
    ]
    md5_lines, sha_lines = [], []
    for name in ("Packages", "Packages.gz"):
        md5, sha, size = _hashes(os.path.join(repo_dir, name))
        md5_lines.append(" %s %d %s" % (md5, size, name))
        sha_lines.append(" %s %d %s" % (sha, size, name))
    lines.append("MD5Sum:")
    lines.extend(md5_lines)
    lines.append("SHA256:")
    lines.extend(sha_lines)
    with open(os.path.join(repo_dir, "Release"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def deb_stanza_text(deb_path: str, control: Stanza) -> str:
    md5, sha, size = _hashes(deb_path)
    extra = "\n".join(
        [
            "Filename: ./%s" % os.path.basename(deb_path),
            "Size: %d" % size,
            "MD5sum: %s" % md5,
            "SHA256: %s" % sha,
        ]
    )
    return control.raw.rstrip() + "\n" + extra


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_bundle(
    rustdesk_deb: str,
    output: str,
    releases: Optional[List[str]] = None,
    mirror: str = DEFAULT_MIRROR,
    extra_packages: Optional[List[str]] = None,
    cache_dir: Optional[str] = None,
    log: LogFn = _noop_log,
    progress: ProgressFn = _noop_progress,
    verify_gpg: str = "auto",
    indices: Optional[Dict[str, PackageIndex]] = None,
) -> str:
    """制作离线包，返回输出文件路径。releases 为 Ubuntu 代号列表（默认 20.04/22.04/24.04）。"""
    releases = list(releases or DEFAULT_RELEASES)
    for r in releases:
        if r not in RELEASES:
            raise BuildError("不支持的 Ubuntu 版本：%s" % r)
    extra_packages = list(EXTRA_PACKAGES if extra_packages is None else extra_packages)
    cache_dir = cache_dir or default_cache_dir()
    if not os.path.isfile(rustdesk_deb):
        raise BuildError("找不到 RustDesk 安装包：%s" % rustdesk_deb)

    control = read_deb_control(rustdesk_deb)
    pkg_name = control.get("Package", "")
    arch = control.get("Architecture", "")
    log("RustDesk 安装包：%s %s（%s）" % (pkg_name, control.get("Version"), arch))
    if pkg_name != "rustdesk":
        log("警告：包名是 %r 而不是 rustdesk" % pkg_name)
    if arch not in (ARCH, "all"):
        raise BuildError("RustDesk 安装包架构是 %s，电脑 B 需要 %s（x86_64）" % (arch, ARCH))
    log("目标系统：%s" % "、".join(release_label(r) for r in releases))

    roots: List[Tuple[List[Relation], str]] = []
    for field in ("Pre-Depends", "Depends"):
        for group in control.relations(field):
            roots.append((group, pkg_name))
    for name in extra_packages:
        roots.append(([Relation(name)], "额外软件包"))

    staging = tempfile.mkdtemp(prefix="ard-bundle-")
    try:
        root = os.path.join(staging, BUNDLE_ROOT)
        pool = os.path.join(root, "pool")
        os.makedirs(pool)
        deb_name = os.path.basename(rustdesk_deb)
        shutil.copyfile(rustdesk_deb, os.path.join(pool, deb_name))
        rustdesk_text = deb_stanza_text(os.path.join(pool, deb_name), control)
        manifest_releases: Dict[str, Dict] = {}

        n = len(releases)
        for i, codename in enumerate(releases):
            label = release_label(codename)

            def sub_progress(stage: str, frac: float, i: int = i, label: str = label) -> None:
                progress("%s：%s" % (label, stage), (i + 0.1 + 0.85 * frac) / n)

            progress("%s：读取软件源索引" % label, i / float(n))
            if indices is not None and codename in indices:
                index = indices[codename]
            else:
                index = load_index(mirror, cache_dir, suites_for(codename), log=log,
                                   verify_gpg=verify_gpg)
            closure = resolve_closure(index, roots)
            del index
            # 不要把 RustDesk 自己从镜像里再下载一遍（官方源里也没有）
            closure = [st for st in closure if st.name != pkg_name]
            total_mb = sum(int(st.get("Size", "0")) for st in closure) / 1048576.0
            log("%s 依赖闭包：%d 个软件包，约 %.1f MB" % (label, len(closure), total_mb))
            _download_debs(mirror, closure, pool, cache_dir, log, sub_progress)

            repo = os.path.join(root, "repos", codename)
            os.makedirs(repo)
            texts = [_local_stanza_text(st) for st in closure] + [rustdesk_text]
            write_local_repo(repo, texts)
            manifest_releases[codename] = {
                "version": RELEASES[codename]["version"],
                "packages": [
                    {
                        "name": st.name,
                        "version": st.version,
                        "file": "pool/" + os.path.basename(st["Filename"]),
                        "size": int(st.get("Size", "0")),
                        "sha256": st.get("SHA256", ""),
                    }
                    for st in closure
                ],
            }

        _md5, rd_sha, rd_size = _hashes(os.path.join(pool, deb_name))
        manifest = {
            "format": BUNDLE_FORMAT,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "builder": "AutoRustDesk %s" % __version__,
            "arch": ARCH,
            "mirror": mirror,
            "rustdesk": {
                "package": pkg_name,
                "version": control.get("Version", ""),
                "file": "pool/" + deb_name,
                "size": rd_size,
                "sha256": rd_sha,
            },
            "install_packages": [pkg_name] + extra_packages,
            "releases": manifest_releases,
        }
        with open(os.path.join(root, MANIFEST_NAME), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        progress("打包", 0.96)
        output = os.path.abspath(output)
        os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
        tmp_out = output + ".part"
        with tarfile.open(tmp_out, "w") as tf:
            tf.add(root, arcname=BUNDLE_ROOT, filter=_tar_filter)
        os.replace(tmp_out, output)
        progress("完成", 1.0)
        log("离线包已生成：%s（%.1f MB）" % (output, os.path.getsize(output) / 1048576.0))
        return output
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _tar_filter(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    ti.mode = 0o755 if ti.isdir() else 0o644
    return ti


def default_output_name(rustdesk_deb: str, releases: Optional[List[str]] = None) -> str:
    try:
        ver = read_deb_control(rustdesk_deb).get("Version", "unknown")
    except Exception:  # noqa: BLE001
        ver = "unknown"
    versions = "-".join(RELEASES[r]["version"] for r in (releases or DEFAULT_RELEASES) if r in RELEASES)
    return "autorustdesk-bundle-rustdesk-%s-ubuntu-%s.tar" % (ver, versions)


def selected_names(stanzas: Iterable[Stanza]) -> Set[str]:
    return {st.name for st in stanzas}
