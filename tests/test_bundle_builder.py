import hashlib
import http.server
import json
import lzma
import os
import tarfile
import threading

import pytest

from autorustdesk.bundle import Bundle
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


@pytest.fixture()
def fake_mirror(tmp_path):
    """在本地 HTTP 服务上搭一个假的 Ubuntu 软件源。"""
    root = tmp_path / "mirror"
    stanzas = []
    for st in iter_stanzas(INDEX):
        if st.get("Architecture") == "i386":
            continue
        deb_path = root / st["Filename"]
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
    dist = root / "dists" / "focal"
    (dist / "main" / "binary-amd64").mkdir(parents=True)
    (dist / rel).write_bytes(packages_xz)
    (dist / "InRelease").write_text(
        "Origin: Ubuntu\nSuite: focal\nSHA256:\n %s %d %s\n"
        % (hashlib.sha256(packages_xz).hexdigest(), len(packages_xz), rel)
    )

    handler = lambda *a, **kw: _Quiet(*a, directory=str(root), **kw)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()


def test_build_bundle_end_to_end(tmp_path, fake_mirror):
    deb = build_deb(str(tmp_path / "rustdesk-1.4.2-x86_64.deb"), RUSTDESK_CONTROL, {}, {})
    cache = str(tmp_path / "cache")
    index = load_index(fake_mirror, cache, suites=["focal"], components=["main"], verify_gpg="no")
    out = build_bundle(
        deb,
        str(tmp_path / "out" / "bundle.tar"),
        mirror=fake_mirror,
        cache_dir=cache,
        index=index,
    )
    b = Bundle.open(out)
    assert b.rustdesk_version == "1.4.2"
    assert b.install_packages == ["rustdesk", "xserver-xorg-video-dummy"]
    with tarfile.open(out) as tf:
        names = tf.getnames()
        assert "autorustdesk-bundle/repo/Packages" in names
        assert "autorustdesk-bundle/repo/Release" in names
        assert "autorustdesk-bundle/repo/rustdesk-1.4.2-x86_64.deb" in names
        packages = tf.extractfile("autorustdesk-bundle/repo/Packages").read().decode()
    # 本地源里的 Filename 都指向仓库根目录
    for st in iter_stanzas(packages):
        assert st["Filename"].startswith("./")
        assert "/" not in st["Filename"][2:]
    names_in_repo = {st["Package"] for st in iter_stanzas(packages)}
    assert {"rustdesk", "libgtk-3-0", "curl", "libcurl4", "xserver-xorg-video-dummy"} <= names_in_repo
    manifest = json.loads(
        tarfile.open(out).extractfile("autorustdesk-bundle/manifest.json").read().decode()
    )
    assert manifest["rustdesk"]["sha256"]
    # 缓存里应该有下载过的包，第二次制作直接复用
    assert os.listdir(os.path.join(cache, "debs"))
