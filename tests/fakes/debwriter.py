"""测试用：用纯 Python 生成 .deb（ar + control.tar.gz + data.tar.xz）。"""

import io
import os
import tarfile
import time
from typing import Dict, Tuple

# 文件内容 -> (bytes, mode)
Files = Dict[str, Tuple[bytes, int]]


def _tar_bytes(files: Files, compression: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:" + compression) as tf:
        dirs = set()
        for path in sorted(files):
            parts = path.strip("/").split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "./" + "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    ti = tarfile.TarInfo(d)
                    ti.type = tarfile.DIRTYPE
                    ti.mode = 0o755
                    ti.mtime = int(time.time())
                    tf.addfile(ti)
        for path, (data, mode) in sorted(files.items()):
            ti = tarfile.TarInfo("./" + path.strip("/"))
            ti.size = len(data)
            ti.mode = mode
            ti.mtime = int(time.time())
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _ar_member(name: str, data: bytes) -> bytes:
    header = "%-16s%-12d%-6d%-6d%-8s%-10d`\n" % (name, int(time.time()), 0, 0, "100644", len(data))
    out = header.encode("ascii") + data
    if len(data) % 2:
        out += b"\n"
    return out


def build_deb(path: str, control: str, data_files: Files, maint_scripts: Dict[str, str]) -> str:
    control_files: Files = {"control": (control.strip().encode() + b"\n", 0o644)}
    for name, text in maint_scripts.items():
        control_files[name] = (text.encode(), 0o755)
    control_tar = _tar_bytes(control_files, "gz")
    data_tar = _tar_bytes(data_files, "xz")
    blob = b"!<arch>\n"
    blob += _ar_member("debian-binary", b"2.0\n")
    blob += _ar_member("control.tar.gz", control_tar)
    blob += _ar_member("data.tar.xz", data_tar)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(blob)
    return path
