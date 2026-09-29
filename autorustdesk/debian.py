"""Debian 包元数据工具：版本比较、依赖关系解析、control 文件解析、读取 .deb。

只依赖标准库，供离线包制作使用。
"""

import io
import os
import re
import shutil
import subprocess
import tarfile
from typing import Dict, Iterator, List, NamedTuple, Optional, Tuple

# ---------------------------------------------------------------------------
# 版本比较（与 dpkg 的 verrevcmp 算法一致）
# ---------------------------------------------------------------------------


def _is_digit(c: str) -> bool:
    return "0" <= c <= "9"


def _is_alpha(c: str) -> bool:
    return ("a" <= c <= "z") or ("A" <= c <= "Z")


def _order(c: str) -> int:
    if _is_digit(c):
        return 0
    if _is_alpha(c):
        return ord(c)
    if c == "~":
        return -1
    if c:
        return ord(c) + 256
    return 0


def _verrevcmp(a: str, b: str) -> int:
    i, j = 0, 0
    la, lb = len(a), len(b)
    while i < la or j < lb:
        first_diff = 0
        while (i < la and not _is_digit(a[i])) or (j < lb and not _is_digit(b[j])):
            ac = _order(a[i]) if i < la else 0
            bc = _order(b[j]) if j < lb else 0
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < la and a[i] == "0":
            i += 1
        while j < lb and b[j] == "0":
            j += 1
        while i < la and j < lb and _is_digit(a[i]) and _is_digit(b[j]):
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < la and _is_digit(a[i]):
            return 1
        if j < lb and _is_digit(b[j]):
            return -1
        if first_diff:
            return first_diff
    return 0


def split_version(version: str) -> Tuple[int, str, str]:
    """拆成 (epoch, upstream, revision)。"""
    version = version.strip()
    epoch = 0
    if ":" in version:
        e, version = version.split(":", 1)
        epoch = int(e) if e else 0
    revision = ""
    if "-" in version:
        version, revision = version.rsplit("-", 1)
    return epoch, version, revision


def version_compare(a: str, b: str) -> int:
    """比较两个 Debian 版本号，返回 <0 / 0 / >0。"""
    ea, ua, ra = split_version(a)
    eb, ub, rb = split_version(b)
    if ea != eb:
        return ea - eb
    r = _verrevcmp(ua, ub)
    if r:
        return r
    return _verrevcmp(ra, rb)


def version_satisfies(version: str, op: Optional[str], wanted: Optional[str]) -> bool:
    if not op or wanted is None:
        return True
    c = version_compare(version, wanted)
    if op == "<<":
        return c < 0
    if op in ("<=", "<"):
        return c <= 0
    if op == "=":
        return c == 0
    if op in (">=", ">"):
        return c >= 0
    if op == ">>":
        return c > 0
    raise ValueError("未知的版本运算符: %r" % op)


# ---------------------------------------------------------------------------
# 依赖关系解析
# ---------------------------------------------------------------------------


class Relation(NamedTuple):
    name: str
    op: Optional[str] = None
    version: Optional[str] = None
    arch_qualifier: Optional[str] = None

    def __str__(self) -> str:
        s = self.name
        if self.arch_qualifier:
            s += ":" + self.arch_qualifier
        if self.op:
            s += " (%s %s)" % (self.op, self.version)
        return s


_REL_RE = re.compile(
    r"^\s*(?P<name>[a-zA-Z0-9][a-zA-Z0-9+.\-]*)"
    r"(?::(?P<archq>[a-zA-Z0-9\-]+))?"
    r"\s*(?:\(\s*(?P<op><<|<=|>=|>>|=|<|>)\s*(?P<ver>[^)\s]+)\s*\))?"
    r"\s*(?:\[[^\]]*\])?"
    r"\s*(?:<[^>]*>\s*)*$"
)


def parse_relations(text: str) -> List[List[Relation]]:
    """解析 Depends 之类的字段，返回 [[alt1, alt2], [pkg], ...]。"""
    groups: List[List[Relation]] = []
    if not text or not text.strip():
        return groups
    for group_text in text.split(","):
        group_text = group_text.strip()
        if not group_text:
            continue
        alts = []
        for alt_text in group_text.split("|"):
            m = _REL_RE.match(alt_text)
            if not m:
                raise ValueError("无法解析依赖关系: %r" % alt_text)
            alts.append(
                Relation(m.group("name"), m.group("op"), m.group("ver"), m.group("archq"))
            )
        groups.append(alts)
    return groups


def format_relations(groups: List[List[Relation]]) -> str:
    return ", ".join(" | ".join(str(r) for r in g) for g in groups)


# ---------------------------------------------------------------------------
# control / Packages 文件解析
# ---------------------------------------------------------------------------


class Stanza(dict):
    """一段 RFC822 风格的记录；保留原始文本以便原样写回 Packages。"""

    raw: str = ""

    @property
    def name(self) -> str:
        return self["Package"]

    @property
    def version(self) -> str:
        return self["Version"]

    def relations(self, field: str) -> List[List[Relation]]:
        return parse_relations(self.get(field, ""))


def iter_stanzas(text: str) -> Iterator[Stanza]:
    lines: List[str] = []
    for line in text.splitlines():
        if line.strip() == "":
            if lines:
                yield _make_stanza(lines)
                lines = []
            continue
        lines.append(line)
    if lines:
        yield _make_stanza(lines)


def _make_stanza(lines: List[str]) -> Stanza:
    st = Stanza()
    key = None
    for line in lines:
        if line[:1] in (" ", "\t"):
            if key is not None:
                st[key] += "\n" + line
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        st[key] = value.strip()
    st.raw = "\n".join(lines)
    return st


def format_stanza(fields: Dict[str, str]) -> str:
    out = []
    for k, v in fields.items():
        if v is None:
            continue
        out.append("%s: %s" % (k, v))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 读取 .deb（ar 归档 + control.tar.*）
# ---------------------------------------------------------------------------


def _iter_ar_members(data: bytes) -> Iterator[Tuple[str, bytes]]:
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("不是有效的 .deb 文件（缺少 ar 头）")
    pos = 8
    while pos + 60 <= len(data):
        header = data[pos:pos + 60]
        name = header[0:16].decode("ascii", "replace").strip()
        size = int(header[48:58].decode("ascii").strip())
        if header[58:60] != b"`\n":
            raise ValueError("损坏的 .deb 文件（ar 成员头错误）")
        pos += 60
        yield name.rstrip("/"), data[pos:pos + size]
        pos += size + (size & 1)


def _decompress_zstd(blob: bytes) -> bytes:
    try:
        import zstandard  # type: ignore

        return zstandard.ZstdDecompressor().decompressobj().decompress(blob)
    except ImportError:
        pass
    if shutil.which("zstd"):
        return subprocess.run(
            ["zstd", "-dc"], input=blob, stdout=subprocess.PIPE, check=True
        ).stdout
    raise RuntimeError("该 .deb 使用 zstd 压缩，请安装 zstd 命令或 Python 包 zstandard")


def read_deb_control(path: str) -> Stanza:
    """读取 .deb 中的 control 文件。"""
    with open(path, "rb") as f:
        data = f.read()
    for name, blob in _iter_ar_members(data):
        if not name.startswith("control.tar"):
            continue
        if name.endswith(".zst"):
            blob = _decompress_zstd(blob)
            mode = "r:"
        elif name.endswith((".gz", ".xz", ".bz2")):
            mode = "r:*"
        else:
            mode = "r:"
        with tarfile.open(fileobj=io.BytesIO(blob), mode=mode) as tf:
            for member in tf.getmembers():
                if os.path.basename(member.name) == "control" and member.isfile():
                    f2 = tf.extractfile(member)
                    assert f2 is not None
                    text = f2.read().decode("utf-8")
                    stanzas = list(iter_stanzas(text))
                    if not stanzas:
                        break
                    return stanzas[0]
    raise ValueError("在 %s 中找不到 control 文件" % path)
