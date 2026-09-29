import os

import pytest

from autorustdesk.debian import (
    Relation,
    iter_stanzas,
    parse_relations,
    read_deb_control,
    version_compare,
    version_satisfies,
)
from tests.fakes.debwriter import build_deb


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("1.0", "1.0", 0),
        ("1.0", "1.1", -1),
        ("1.10", "1.9", 1),
        ("1.0~rc1", "1.0", -1),
        ("1.0", "1.0+b1", -1),
        ("1:0.9", "2.0", 1),
        ("2.31-0ubuntu9", "2.31-0ubuntu9.2", -1),
        ("2:1.20.13-1ubuntu1~20.04.8", "2:1.18.99.901", 1),
        ("1.0-1", "1.0", 1),
        ("0.2.7-1", "0.2.7-1", 0),
        ("3.24.20-0ubuntu1", "3.24.20-0ubuntu1.1", -1),
        ("1.0a", "1.0", 1),
        ("1.0~", "1.0", -1),
        ("1.0~~", "1.0~", -1),
    ],
)
def test_version_compare(a, b, expected):
    r = version_compare(a, b)
    assert (r > 0) - (r < 0) == expected
    r2 = version_compare(b, a)
    assert (r2 > 0) - (r2 < 0) == -expected


def test_version_satisfies():
    assert version_satisfies("2.31-0ubuntu9", ">=", "2.17")
    assert not version_satisfies("1.0", ">>", "1.0")
    assert version_satisfies("1.0", "<=", "1.0")
    assert version_satisfies("1.0", "<<", "1.0.1")
    assert version_satisfies("1.0", None, None)


def test_parse_relations_rustdesk_depends():
    text = (
        "libgtk-3-0t64 | libgtk-3-0, libxcb-randr0, libxdo3 | libxdo4, "
        "libc6 (>= 2.17), python3:any (>= 3.6~), foo [amd64], bar <!nocheck>"
    )
    groups = parse_relations(text)
    assert groups[0] == [Relation("libgtk-3-0t64"), Relation("libgtk-3-0")]
    assert groups[3] == [Relation("libc6", ">=", "2.17")]
    assert groups[4] == [Relation("python3", ">=", "3.6~", "any")]
    assert groups[5] == [Relation("foo")]
    assert groups[6] == [Relation("bar")]


def test_iter_stanzas_continuation():
    text = "Package: a\nDescription: short\n long line\n .\n more\n\nPackage: b\nVersion: 1\n"
    sts = list(iter_stanzas(text))
    assert [s["Package"] for s in sts] == ["a", "b"]
    assert "long line" in sts[0]["Description"]
    assert sts[0].raw.startswith("Package: a")


def test_read_deb_control(tmp_path):
    path = os.path.join(tmp_path, "x.deb")
    build_deb(
        path,
        "Package: rustdesk\nVersion: 1.4.2\nArchitecture: amd64\nDepends: curl, libc6\n"
        "Description: test\n",
        {"usr/share/x/readme": (b"hi", 0o644)},
        {"postinst": "#!/bin/sh\nexit 0\n"},
    )
    st = read_deb_control(path)
    assert st["Package"] == "rustdesk"
    assert st["Version"] == "1.4.2"
    assert st.relations("Depends") == [[Relation("curl")], [Relation("libc6")]]
