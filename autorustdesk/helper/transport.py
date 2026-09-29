"""网络助手与界面之间的连接。

Linux 上 pkexec 会保留标准输入输出，界面和助手直接用管道通信。
macOS（osascript 管理员授权）和 Windows（UAC）提权后，助手在后台单独运行，拿不到界面的
管道，于是改为：界面在 127.0.0.1 上监听一个随机端口，助手连回来。

双方用令牌互相验证身份：令牌写在只有当前用户（和管理员）能读的临时文件里，文件路径通过
命令行传给助手；连接建立后双方各出一个随机数，对方用令牌算 HMAC 作答，令牌本身不经过网络。
"""

import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import time
from typing import BinaryIO, Callable, Optional, TextIO, Tuple


class TransportError(Exception):
    pass


def _proof(token: str, role: str, nonce: str) -> str:
    return hmac.new(token.encode(), ("%s:%s" % (role, nonce)).encode(), hashlib.sha256).hexdigest()


def _send(sock: socket.socket, obj: dict) -> None:
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def _drop(sock: socket.socket) -> None:
    """立即断开（还有 makefile 文件对象时，单纯 close 不会真正关闭连接）。"""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


def _read(f, limit: int = 4096) -> dict:
    line = f.readline(limit)
    if not line:
        raise TransportError("对方断开了连接")
    try:
        obj = json.loads(line)
    except ValueError:
        raise TransportError("收到无法识别的数据")
    if not isinstance(obj, dict):
        raise TransportError("收到无法识别的数据")
    return obj


class Listener:
    """界面一侧：监听本机端口，等待助手连回来。"""

    def __init__(self) -> None:
        self.token = secrets.token_hex(32)
        self.dir = tempfile.mkdtemp(prefix="autorustdesk-")
        self.token_file = os.path.join(self.dir, "token")
        fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.token)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # 防止别的程序用 SO_REUSEADDR 抢占同一端口
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.address = "127.0.0.1:%d" % self.sock.getsockname()[1]

    def accept(self, timeout: float, alive: Callable[[], bool]) -> Tuple[socket.socket, BinaryIO]:
        """等待并验证助手的连接。alive() 返回 False 表示助手已经不可能连上来（例如用户取消了授权）。"""
        deadline = time.time() + timeout
        self.sock.settimeout(0.3)
        while time.time() < deadline:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                if not alive():
                    raise TransportError("网络助手没有启动")
                continue
            try:
                return conn, self._handshake(conn)
            except (TransportError, OSError, ValueError):
                _drop(conn)
        raise TransportError("等待网络助手超时")

    def _handshake(self, conn: socket.socket) -> BinaryIO:
        conn.settimeout(10)
        f = conn.makefile("rb")
        hello = _read(f)
        if hello.get("type") != "auth" or not hello.get("nonce"):
            raise TransportError("不是网络助手")
        nonce = secrets.token_hex(16)
        _send(conn, {"type": "auth", "proof": _proof(self.token, "gui", str(hello["nonce"])),
                     "nonce": nonce})
        ok = _read(f)
        if not hmac.compare_digest(str(ok.get("proof", "")), _proof(self.token, "helper", nonce)):
            raise TransportError("网络助手验证失败")
        conn.settimeout(None)
        return f

    def close(self) -> None:
        try:
            self.sock.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)


def connect_back(address: str, token_file: str, timeout: float = 30) -> Tuple[socket.socket, TextIO, TextIO]:
    """助手一侧：连回界面并验证身份，返回 (套接字, 读, 写)。"""
    with open(token_file, encoding="utf-8") as f:
        token = f.read().strip()
    host, _, port = address.rpartition(":")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise TransportError("只允许连接本机")
    sock = socket.create_connection((host, int(port)), timeout=timeout)
    try:
        rfile = sock.makefile("r", encoding="utf-8", newline="\n")
        nonce = secrets.token_hex(16)
        _send(sock, {"type": "auth", "nonce": nonce})
        reply = _read(rfile)
        if not hmac.compare_digest(str(reply.get("proof", "")), _proof(token, "gui", nonce)):
            raise TransportError("界面验证失败")
        _send(sock, {"type": "auth_ok", "proof": _proof(token, "helper", str(reply.get("nonce", "")))})
        sock.settimeout(None)
        wfile = sock.makefile("w", encoding="utf-8", newline="\n")
        return sock, rfile, wfile
    except BaseException:
        _drop(sock)
        raise


def parse_args(argv) -> Tuple[Optional[str], Optional[str]]:
    """从助手命令行里取出 --connect 地址和 --token-file 路径。"""
    address = token_file = None
    args = list(argv)
    for i, a in enumerate(args):
        if a == "--connect" and i + 1 < len(args):
            address = args[i + 1]
        elif a == "--token-file" and i + 1 < len(args):
            token_file = args[i + 1]
    return address, token_file
