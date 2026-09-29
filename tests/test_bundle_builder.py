import hashlib
import http.server
import json
import lzma
import os
import tarfile
import threading

import pytest

from autorustdesk.bundle import Bundle, BundleError, parse_releases
from autorustdesk.bundle.builder import (
    PackageIndex,
    build_bundle,
    load_index,
    parse_release_hashes,
    resolve_closure,
)
from autorustdesk.debian import Relation, iter_stanzas
from tests.fakes.debwriter import build_deb

INDEX = """\
Package: libc6
Version: 2.31-0ubuntu9
Architecture: amd64
Priority: required
Filename: pool/main/g/glibc/libc6_2.31-0ubuntu9_amd64.deb

Package: libc6
Version: 2.31-0ubuntu9.9
Architecture: amd64
Priority: required
Filename: pool/main/g/glibc/libc6_2.31-0ubuntu9.9_amd64.deb

Package: libgtk-3-0
Version: 3.24.20-0ubuntu1
Architecture: amd64
Depends: libc6 (>= 2.29), adwaita-icon-theme | gnome-icon-theme, libgtk-3-common (>= 3.24.20-0ubuntu1)
Filename: pool/main/g/gtk+3.0/libgtk-3-0_3.24.20-0ubuntu1_amd64.deb

Package: libgtk-3-common
Version: 3.24.20-0ubuntu1
Architecture: all
Depends: dconf-gsettings-backend | gsettings-backend
Filename: pool/main/g/gtk+3.0/libgtk-3-common_3.24.20-0ubuntu1_all.deb

Package: adwaita-icon-theme
Version: 3.36.1-2ubuntu0.20.04.2
Architecture: all
Filename: pool/main/a/adwaita-icon-theme/adwaita-icon-theme_3.36.1-2ubuntu0.20.04.2_all.deb

Package: dconf-gsettings-backend
Version: 0.36.0-1
Architecture: amd64
Provides: gsettings-backend
Depends: libc6
Filename: pool/main/d/dconf/dconf-gsettings-backend_0.36.0-1_amd64.deb

Package: curl
Version: 7.68.0-1ubuntu2.22
Architecture: amd64
Depends: libcurl4 (= 7.68.0-1ubuntu2.22)
Filename: pool/main/c/curl/curl_7.68.0-1ubuntu2.22_amd64.deb

Package: libcurl4
Version: 7.68.0-1ubuntu2.22
Architecture: amd64
Depends: libc6
Filename: pool/main/c/curl/libcurl4_7.68.0-1ubuntu2.22_amd64.deb

Package: debconf
Version: 1.5.73
Architecture: all
Priority: required
Provides: debconf-2.0
Filename: pool/main/d/debconf/debconf_1.5.73_all.deb

Package: cdebconf
Version: 0.251ubuntu1
Architecture: amd64
Priority: extra
Provides: debconf-2.0
Filename: pool/universe/c/cdebconf/cdebconf_0.251ubuntu1_amd64.deb

Package: xserver-xorg-core
Version: 2:1.20.13-1ubuntu1~20.04.8
Architecture: amd64
Provides: xorg-video-abi-24, xorg-input-abi-24
Depends: libc6, debconf (>= 0.5) | debconf-2.0
Filename: pool/main/x/xorg-server/xserver-xorg-core_1.20.13-1ubuntu1~20.04.8_amd64.deb

Package: xserver-xorg-video-dummy
Version: 1:0.3.8-1build3
Architecture: amd64
Depends: libc6 (>= 2.4), xorg-video-abi-24, xserver-xorg-core (>= 2:1.18.99.901)
Filename: pool/main/x/xserver-xorg-video-dummy/xserver-xorg-video-dummy_0.3.8-1build3_amd64.deb

Package: somethingi386
Version: 1
Architecture: i386
Filename: pool/x.deb
"""

RUSTDESK_CONTROL = (
    "Package: rustdesk\nVersion: 1.4.2\nArchitecture: amd64\n"
    "Depends: libgtk-3-0t64 | libgtk-3-0, curl\nDescription: test\n"
)


def _index(text=INDEX):
    idx = PackageIndex()
    idx.add_text(text)
    return idx


def test_index_keeps_all_versions_and_skips_foreign_arch():
    idx = _index()
    assert [s.version for s in idx.by_name["libc6"]] == ["2.31-0ubuntu9.9", "2.31-0ubuntu9"]
    assert "somethingi386" not in idx.by_name


def test_resolve_closure_alternatives_virtual_and_versions():
    idx = _index()
    roots = [
        ([Relation("libgtk-3-0t64"), Relation("libgtk-3-0")], "rustdesk"),
        ([Relation("curl")], "rustdesk"),
        ([Relation("xserver-xorg-video-dummy")], "extra"),
    ]
    closure = resolve_closure(idx, roots)
    names = {s.name: s.version for s in closure}
    assert names["libgtk-3-0"] == "3.24.20-0ubuntu1"
    assert names["libc6"] == "2.31-0ubuntu9.9"
    # 虚拟包 gsettings-backend 由 dconf-gsettings-backend 提供
    assert "dconf-gsettings-backend" in names
    # 虚拟包 xorg-video-abi-24 由 xserver-xorg-core 提供
    assert "xserver-xorg-core" in names
    # debconf | debconf-2.0：选真实包 debconf，而不是 cdebconf
    assert "debconf" in names and "cdebconf" not in names
    assert "gnome-icon-theme" not in names


def test_unsatisfiable_dependency_is_reported():
    idx = _index()
    from autorustdesk.bundle.builder import BuildError

    with pytest.raises(BuildError) as e:
        resolve_closure(idx, [([Relation("does-not-exist")], "rustdesk")])
    assert "does-not-exist" in str(e.value)


def test_parse_release_hashes():
    text = "Origin: Ubuntu\nSHA256:\n abc 10 main/binary-amd64/Packages.xz\n def 20 main/x\nFoo: bar\n"
    h = parse_release_hashes(text)
    assert h["main/binary-amd64/Packages.xz"] == ("abc", 10)
    assert h["main/x"] == ("def", 20)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


# 22.04 的索引：GTK 版本不同，其余包（例如图标主题）与 20.04 同名同内容
INDEX_JAMMY = INDEX.replace("3.24.20-0ubuntu1", "3.24.33-1ubuntu2")


def _publish(root, codename, index_text):
    stanzas = []
    for st in iter_stanzas(index_text):
        if st.get("Architecture") == "i386":
            continue
        deb_path = root / st["Filename"]
        if not deb_path.exists():
            build_deb(
                str(deb_path),
                "Package: %s\nVersion: %s\nArchitecture: %s\nDescription: x\n"
                % (st["Package"], st["Version"], st["Architecture"]),
                {"usr/share/doc/%s/x" % st["Package"]: (b"x", 0o644)},
                {},
            )
        data = deb_path.read_bytes()
        stanzas.append(
            st.raw + "\nSize: %d\nSHA256: %s" % (len(data), hashlib.sha256(data).hexdigest())
        )
    packages_xz = lzma.compress(("\n\n".join(stanzas) + "\n").encode())
    rel = "main/binary-amd64/Packages.xz"
    dist = root / "dists" / codename
    (dist / "main" / "binary-amd64").mkdir(parents=True)
    (dist / rel).write_bytes(packages_xz)
    (dist / "InRelease").write_text(
        "Origin: Ubuntu\nSuite: %s\nSHA256:\n %s %d %s\n"
        % (codename, hashlib.sha256(packages_xz).hexdigest(), len(packages_xz), rel)
    )


@pytest.fixture()
def fake_mirror(tmp_path):
    """在本地 HTTP 服务上搭一个假的 Ubuntu 软件源（含 focal 和 jammy）。"""
    root = tmp_path / "mirror"
    _publish(root, "focal", INDEX)
    _publish(root, "jammy", INDEX_JAMMY)
    handler = lambda *a, **kw: _Quiet(*a, directory=str(root), **kw)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()


def _indices(mirror, cache, releases):
    return {r: load_index(mirror, cache, suites=[r], components=["main"], verify_gpg="no")
            for r in releases}


def _repo_packages(tar_path, member):
    with tarfile.open(tar_path) as tf:
        return tf.extractfile(member).read().decode()


def test_build_bundle_single_release(tmp_path, fake_mirror):
    deb = build_deb(str(tmp_path / "rustdesk-1.4.2-x86_64.deb"), RUSTDESK_CONTROL, {}, {})
    cache = str(tmp_path / "cache")
    out = build_bundle(deb, str(tmp_path / "out" / "bundle.tar"), releases=["focal"],
                       mirror=fake_mirror, cache_dir=cache,
                       indices=_indices(fake_mirror, cache, ["focal"]))
    b = Bundle.open(out)
    assert b.format == 2
    assert b.rustdesk_version == "1.4.2"
    assert b.install_packages == ["rustdesk", "xserver-xorg-video-dummy"]
    assert list(b.releases) == ["focal"]
    with tarfile.open(out) as tf:
        names = tf.getnames()
    assert "autorustdesk-bundle/repos/focal/Packages" in names
    assert "autorustdesk-bundle/repos/focal/Release" in names
    assert "autorustdesk-bundle/pool/rustdesk-1.4.2-x86_64.deb" in names
    packages = _repo_packages(out, "autorustdesk-bundle/repos/focal/Packages")
    # 本地源里的 Filename 都指向仓库根目录
    for st in iter_stanzas(packages):
        assert st["Filename"].startswith("./")
        assert "/" not in st["Filename"][2:]
    names_in_repo = {st["Package"] for st in iter_stanzas(packages)}
    assert {"rustdesk", "libgtk-3-0", "curl", "libcurl4", "xserver-xorg-video-dummy"} <= names_in_repo
    assert b.manifest["rustdesk"]["sha256"]
    # 缓存里应该有下载过的包，第二次制作直接复用
    assert os.listdir(os.path.join(cache, "debs"))


def test_build_bundle_multi_release_and_extract_per_release(tmp_path, fake_mirror):
    deb = build_deb(str(tmp_path / "rustdesk-1.4.2-x86_64.deb"), RUSTDESK_CONTROL, {}, {})
    cache = str(tmp_path / "cache")
    out = build_bundle(deb, str(tmp_path / "bundle.tar"), releases=["focal", "jammy"],
                       mirror=fake_mirror, cache_dir=cache,
                       indices=_indices(fake_mirror, cache, ["focal", "jammy"]))
    b = Bundle.open(out)
    assert set(b.releases) == {"focal", "jammy"}
    assert "20.04" in b.summary() and "22.04" in b.summary()
    with tarfile.open(out) as tf:
        pool = [n for n in tf.getnames() if n.startswith("autorustdesk-bundle/pool/")]
    # 两个版本共用的包只存一份，不同版本的 GTK 各一份
    assert sum("adwaita-icon-theme" in n for n in pool) == 1
    assert sum("libgtk-3-0_" in n for n in pool) == 2

    for codename, gtk in (("focal", "3.24.20-0ubuntu1"), ("jammy", "3.24.33-1ubuntu2")):
        sub = str(tmp_path / ("%s.tar" % codename))
        b.write_release_tar(codename, sub)
        with tarfile.open(sub) as tf:
            names = set(tf.getnames())
            manifest = json.loads(tf.extractfile("autorustdesk-bundle/manifest.json").read())
            packages = tf.extractfile("autorustdesk-bundle/repo/Packages").read().decode()
        # 给 B 的是单版本（格式 1）包：B 端脚本不用区分格式
        assert manifest["format"] == 1
        assert manifest["target"]["codename"] == codename
        assert manifest["rustdesk"]["file"] == "repo/rustdesk-1.4.2-x86_64.deb"
        stanzas = list(iter_stanzas(packages))
        assert any(st["Package"] == "libgtk-3-0" and st["Version"] == gtk for st in stanzas)
        for st in stanzas:
            assert "autorustdesk-bundle/repo/" + st["Filename"][2:] in names
        assert not any("/pool/" in n or "/repos/" in n for n in names)

    # 目录形式的离线包也能取出单版本包
    extracted = tmp_path / "dir"
    with tarfile.open(out) as tf:
        tf.extractall(extracted)
    d = Bundle.open(str(extracted))
    d.write_release_tar("jammy", str(tmp_path / "jammy2.tar"))
    assert "libgtk-3-0_3.24.33" in " ".join(tarfile.open(str(tmp_path / "jammy2.tar")).getnames())

    with pytest.raises(BundleError):
        b.write_release_tar("noble", str(tmp_path / "noble.tar"))


def test_old_format1_bundle_still_works(tmp_path):
    """第一版程序做的离线包（只含 20.04，格式 1）仍能使用。"""
    root = tmp_path / "old" / "autorustdesk-bundle"
    (root / "repo").mkdir(parents=True)
    (root / "repo" / "Packages").write_text("Package: rustdesk\nVersion: 1.4.2\nFilename: ./rd.deb\n")
    (root / "repo" / "rd.deb").write_bytes(b"deb")
    (root / "manifest.json").write_text(json.dumps({
        "format": 1, "rustdesk": {"version": "1.4.2", "file": "repo/rd.deb"},
        "target": {"distro": "ubuntu", "release": "20.04", "codename": "focal", "arch": "amd64"},
        "install_packages": ["rustdesk"], "packages": [],
    }))
    old_tar = str(tmp_path / "old.tar")
    with tarfile.open(old_tar, "w") as tf:
        tf.add(str(root), arcname="autorustdesk-bundle")
    for path in (old_tar, str(tmp_path / "old")):
        b = Bundle.open(path)
        assert list(b.releases) == ["focal"]
        sub = str(tmp_path / "sub.tar")
        b.write_release_tar("focal", sub)
        names = set(tarfile.open(sub).getnames())
        assert {"autorustdesk-bundle/manifest.json", "autorustdesk-bundle/repo/Packages",
                "autorustdesk-bundle/repo/rd.deb"} <= names
        with pytest.raises(BundleError):
            b.write_release_tar("jammy", sub)


def test_parse_releases():
    assert parse_releases("20.04, 22.04,noble") == ["focal", "jammy", "noble"]
    assert parse_releases("26.04") == ["resolute"]
    with pytest.raises(BundleError):
        parse_releases("18.04")
