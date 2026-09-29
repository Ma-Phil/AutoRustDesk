"""通过 SSH 操作电脑 B：执行命令（含 sudo）、上传文件、运行 ard_remote.py。"""

import json
import os
import posixpath
import shlex
import socket
import time
from typing import Callable, Dict, List, Optional, Tuple

import paramiko

from ..remote import REMOTE_SCRIPT

LineFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]


class SshError(Exception):
    pass


class AuthError(SshError):
    pass


class SudoError(SshError):
    pass


class HostKeyMismatch(SshError):
    def __init__(self, expected: str, actual: str):
        super().__init__("主机密钥与记录不一致")
        self.expected = expected
        self.actual = actual


class _RecordPolicy(paramiko.MissingHostKeyPolicy):
    """不使用 known_hosts，由我们按设备 MAC 自己核对。"""

    def missing_host_key(self, client, hostname, key):
        return


def format_host_key(key: paramiko.PKey) -> str:
    return "%s %s" % (key.get_name(), key.get_base64())


def fingerprint(host_key: str) -> str:
    import base64
    import hashlib

    try:
        blob = base64.b64decode(host_key.split()[1])
    except (IndexError, ValueError):
        return host_key
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


class RemoteSession:
    def __init__(self, host: str, username: str, password: str = "", port: int = 22,
                 key_filename: str = "", timeout: float = 15):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_filename = key_filename
        self.timeout = timeout
        self.client: Optional[paramiko.SSHClient] = None
        self.host_key = ""
        self.workdir = ""

    # ------------------------------------------------------------------ 连接

    def connect(self, expected_host_key: str = "") -> str:
        """连接并返回服务器主机密钥；与 expected_host_key 不一致时抛 HostKeyMismatch。"""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise SshError("无法连接 %s:%d（%s）" % (self.host, self.port, e))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_RecordPolicy())
        try:
            client.connect(
                self.host, port=self.port, username=self.username,
                password=self.password or None,
                key_filename=self.key_filename or None,
                sock=sock, timeout=self.timeout, banner_timeout=self.timeout,
                auth_timeout=self.timeout, allow_agent=not self.password,
                look_for_keys=not self.password,
            )
        except paramiko.AuthenticationException as e:
            client.close()
            raise AuthError("SSH 登录失败：用户名或密码不对（%s）" % e)
        except (paramiko.SSHException, OSError) as e:
            client.close()
            raise SshError("SSH 连接失败：%s" % e)
        transport = client.get_transport()
        assert transport is not None
        transport.set_keepalive(15)
        self.host_key = format_host_key(transport.get_remote_server_key())
        if expected_host_key and expected_host_key != self.host_key:
            client.close()
            raise HostKeyMismatch(expected_host_key, self.host_key)
        self.client = client
        return self.host_key

    def close(self) -> None:
        if self.client:
            self.client.close()
        self.client = None

    # ------------------------------------------------------------------ 执行

    def exec(self, command: str, sudo: bool = False, on_line: Optional[LineFn] = None,
             timeout: float = 300) -> Tuple[int, str]:
        """执行命令，返回 (退出码, 合并后的输出)。sudo=True 时用 sudo -S 从标准输入送密码。"""
        if not self.client:
            raise SshError("SSH 未连接")
        if sudo:
            command = "sudo -S -p '' -- " + command
        transport = self.client.get_transport()
        assert transport is not None
        chan = transport.open_session()
        chan.set_combine_stderr(True)
        chan.settimeout(1.0)
        chan.exec_command(command)
        if sudo:
            chan.sendall(((self.password or "") + "\n").encode())
        chan.shutdown_write()
        buf = b""
        lines: List[str] = []
        deadline = time.time() + timeout
        while True:
            try:
                data = chan.recv(65536)
            except socket.timeout:
                data = None
            if data:
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    lines.append(line)
                    if on_line:
                        on_line(line)
            elif data == b"" or chan.exit_status_ready():
                if chan.exit_status_ready() and not chan.recv_ready():
                    break
                time.sleep(0.05)
            if time.time() > deadline:
                chan.close()
                raise SshError("命令超时（%ds）：%s" % (timeout, command[:120]))
        if buf:
            line = buf.decode("utf-8", "replace")
            lines.append(line)
            if on_line:
                on_line(line)
        rc = chan.recv_exit_status()
        chan.close()
        out = "\n".join(lines)
        if sudo and rc != 0:
            low = out.lower()
            if "incorrect password" in low or "sorry, try again" in low or "no password was provided" in low:
                raise SudoError("sudo 密码不对")
            if "not in the sudoers" in low or "may not run sudo" in low:
                raise SudoError("账号 %s 没有 sudo 权限" % self.username)
        return rc, out

    def check_sudo(self) -> None:
        rc, out = self.exec("true", sudo=True, timeout=30)
        if rc != 0:
            raise SudoError("无法使用 sudo：%s" % out.strip()[-300:])

    # ------------------------------------------------------------------ 文件

    def make_workdir(self) -> str:
        rc, out = self.exec("mktemp -d /tmp/autorustdesk-XXXXXX", timeout=30)
        path = out.strip().splitlines()[-1] if out.strip() else ""
        if rc != 0 or not path.startswith("/tmp/autorustdesk-"):
            raise SshError("无法在 B 上创建临时目录：%s" % out.strip())
        self.exec("chmod 755 %s" % shlex.quote(path))
        self.workdir = path
        return path

    def put(self, local: str, remote: str, progress: Optional[ProgressFn] = None) -> None:
        assert self.client
        sftp = self.client.open_sftp()
        try:
            sftp.put(local, remote, callback=progress)
            size = os.path.getsize(local)
            if sftp.stat(remote).st_size != size:
                raise SshError("上传不完整：%s" % remote)
        finally:
            sftp.close()

    def write_file(self, remote: str, data: bytes, mode: int = 0o600) -> None:
        assert self.client
        sftp = self.client.open_sftp()
        try:
            with sftp.open(remote, "wb") as f:
                f.chmod(mode)
                f.write(data)
        finally:
            sftp.close()

    def cleanup(self) -> None:
        if self.workdir and self.client:
            try:
                self.exec("rm -rf %s" % shlex.quote(self.workdir), sudo=True, timeout=120)
            except SshError:
                pass
            self.workdir = ""

    # ------------------------------------------------------------------ ard_remote.py

    def upload_remote_script(self) -> str:
        if not self.workdir:
            self.make_workdir()
        remote = posixpath.join(self.workdir, "ard_remote.py")
        self.put(REMOTE_SCRIPT, remote)
        return remote

    def run_ard(self, subcommand: str, args: List[str], on_log: Optional[LineFn] = None,
                on_step: Optional[LineFn] = None, on_output: Optional[LineFn] = None,
                timeout: float = 600) -> Dict:
        """以 sudo 运行 ard_remote.py 的子命令，返回其 ARD:RESULT。"""
        script = posixpath.join(self.workdir, "ard_remote.py") if self.workdir else ""
        if not script:
            script = self.upload_remote_script()
        cmd = "python3 %s %s %s" % (
            shlex.quote(script), subcommand, " ".join(shlex.quote(a) for a in args))
        result: Dict = {}

        def on_line(line: str) -> None:
            if line.startswith("ARD:RESULT "):
                try:
                    result.update(json.loads(line[len("ARD:RESULT "):]))
                except ValueError:
                    pass
            elif line.startswith("ARD:LOG "):
                if on_log:
                    on_log(line[len("ARD:LOG "):])
            elif line.startswith("ARD:STEP "):
                if on_step:
                    on_step(line[len("ARD:STEP "):])
            elif on_output and line.strip():
                on_output(line)

        rc, out = self.exec(cmd, sudo=True, on_line=on_line, timeout=timeout)
        if not result:
            tail = "\n".join(out.strip().splitlines()[-10:])
            raise SshError("B 上的脚本异常退出（%d）：%s" % (rc, tail))
        if not result.get("ok"):
            raise SshError(result.get("error") or "B 上的操作失败")
        return result
