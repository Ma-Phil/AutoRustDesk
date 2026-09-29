"""启动并调用以管理员权限运行的网络助手。

- Linux：pkexec 弹出系统授权框，通过标准输入输出通信。
- macOS：osascript 弹出系统的管理员密码框，助手在后台运行并连回界面（见 helper/transport.py）。
- Windows：弹出 UAC 确认框，同样连回界面。
本程序本身已经以管理员/root 运行时直接启动助手，不再弹框。
"""

import json
import os
import queue
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Optional

from ..helper import HELPER_PROTOCOL
from ..helper.state import STATE_DIR
from ..helper.transport import Listener, TransportError

EventFn = Callable[[Dict[str, Any]], None]
LogFn = Callable[[str, str], None]

AUTH_PROMPT = "AutoRustDesk 需要管理员权限来配置直连网卡。"
CREATE_NO_WINDOW = 0x08000000


class HelperError(Exception):
    pass


def helper_base_command() -> List[str]:
    """启动助手的命令（不含提权）。"""
    if getattr(sys, "frozen", False):
        appimage = os.environ.get("APPIMAGE")
        if appimage and os.path.isfile(appimage):
            # AppImage 运行时挂载在只有当前用户能访问的临时目录里，root 进不去，
            # 所以让 pkexec 直接运行 AppImage 文件本身（以 root 身份重新挂载）
            return [appimage, "helper"]
        return [sys.executable, "helper"]
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "helper", "__main__.py")
    return [os.path.abspath(sys.executable), script]


def is_privileged() -> bool:
    if sys.platform == "win32":
        from ..helper import winnet

        return winnet.is_admin()
    return os.geteuid() == 0


def helper_command() -> List[str]:
    """Linux：带 pkexec/sudo 的完整命令（其它系统用 _launch_elevated 提权）。"""
    base = helper_base_command()
    if not sys.platform.startswith("linux") or os.geteuid() == 0:
        return base
    if shutil.which("pkexec"):
        return ["pkexec"] + base
    if shutil.which("sudo") and sys.stdin is not None and sys.stdin.isatty():
        return ["sudo"] + base
    raise HelperError("需要管理员权限：请安装 policykit-1（pkexec），或用 sudo 运行本程序")


def applescript_quote(text: str) -> str:
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


def macos_auth_script(cmd: List[str]) -> str:
    """osascript 脚本：以管理员身份在后台运行 cmd（输出重定向后 do shell script 立即返回）。"""
    shell = " ".join(shlex.quote(c) for c in cmd) + " >/dev/null 2>&1 &"
    return "do shell script %s with prompt %s with administrator privileges" % (
        applescript_quote(shell), applescript_quote(AUTH_PROMPT))


class _Launch:
    """提权启动的助手进程（或授权程序）的状态。"""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None  # osascript 或直接启动的助手
        self.win_handle = 0  # Windows 提权启动的助手进程句柄
        self.expect_exit = False  # osascript 授权完成后会退出，这是正常的
        self._exited_at = 0.0

    def alive(self) -> bool:
        if self.win_handle:
            from ..helper import winnet

            return winnet.process_exit_code(self.win_handle) is None
        if self.proc is None:
            return False
        rc = self.proc.poll()
        if rc is None:
            return True
        if not (self.expect_exit and rc == 0):
            return False
        # 授权成功、助手已在后台启动，最多再等 30 秒它连回来
        self._exited_at = self._exited_at or time.time()
        return time.time() - self._exited_at < 30

    def failure(self) -> str:
        if self.proc is not None and self.proc.poll() not in (None, 0):
            err = (self.proc.stderr.read() if self.proc.stderr else b"").decode("utf-8", "replace")
            if "-128" in err or "canceled" in err.lower() or "cancelled" in err.lower():
                return "没有获得管理员授权（取消了密码框）"
            return "网络助手启动失败：%s" % (err.strip() or "退出码 %s（详见 %s）" % (
                self.proc.returncode, os.path.join(STATE_DIR, "helper.log")))
        return "网络助手没有启动（详见 %s）" % os.path.join(STATE_DIR, "helper.log")

    def close(self) -> None:
        if self.win_handle:
            from ..helper import winnet

            winnet.close_handle(self.win_handle)
            self.win_handle = 0


class HelperClient:
    def __init__(self, on_event: EventFn, on_log: LogFn):
        self.on_event = on_event
        self.on_log = on_log
        self.proc: Optional[subprocess.Popen] = None  # Linux：助手进程
        self.conn: Optional[socket.socket] = None  # macOS/Windows：助手连回来的连接
        self._reader: Optional[BinaryIO] = None
        self._launch: Optional[_Launch] = None
        self._eof = threading.Event()
        self._responses: Dict[int, "queue.Queue[Dict]"] = {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stderr: List[str] = []

    @property
    def running(self) -> bool:
        if self.conn is not None:
            return not self._eof.is_set()
        return self.proc is not None and self.proc.poll() is None

    # ------------------------------------------------------------------ 启动

    def start(self, timeout: float = 180) -> None:
        if self.running:
            return
        if self.conn is not None:  # 上一个助手已经退出，清理连接
            self._stop_connection()
        self._ready.clear()
        # 每个助手连接用自己的结束标志，旧连接的读线程晚退出也不会影响新连接
        self._eof = threading.Event()
        if sys.platform.startswith("linux"):
            self._start_pipe(timeout)
        else:
            self._start_connect_back(timeout)

    def _start_pipe(self, timeout: float) -> None:
        cmd = helper_command()
        self.on_log("启动网络助手（需要管理员授权）：%s" % " ".join(os.path.basename(c) for c in cmd), "debug")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._read_messages, args=(self.proc.stdout, self._eof), daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._ready.wait(0.2):
                return
            if self.proc.poll() is not None:
                break
        rc = self.proc.poll()
        err = "\n".join(self._stderr[-5:]).strip()
        self.stop()
        if rc in (126, 127):
            raise HelperError("没有获得管理员授权（取消了密码框或密码错误）")
        raise HelperError("网络助手启动失败（退出码 %s）%s" % (rc, "：" + err if err else ""))

    def _start_connect_back(self, timeout: float) -> None:
        listener = Listener()
        try:
            cmd = helper_base_command() + ["--connect", listener.address, "--token-file", listener.token_file]
            self.on_log("启动网络助手（需要管理员授权）", "debug")
            launch = self._launch_elevated(cmd)
            self._launch = launch
            try:
                conn, reader = listener.accept(timeout, launch.alive)
            except TransportError as e:
                reason = launch.failure() if not launch.alive() else str(e)
                raise HelperError(reason)
        finally:
            listener.close()
        self.conn = conn
        self._reader = reader
        threading.Thread(target=self._read_messages, args=(reader, self._eof), daemon=True).start()
        if not self._ready.wait(30):
            self.stop()
            raise HelperError("网络助手没有响应")

    def _launch_elevated(self, cmd: List[str]) -> _Launch:
        launch = _Launch()
        if is_privileged():
            kwargs: Dict[str, Any] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = CREATE_NO_WINDOW
            # 助手的错误信息写在状态目录的 helper.log 里
            launch.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, **kwargs)
            return launch
        if sys.platform == "darwin":
            launch.expect_exit = True
            launch.proc = subprocess.Popen(["osascript", "-e", macos_auth_script(cmd)],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.PIPE)
            return launch
        if sys.platform == "win32":
            from ..helper import winnet

            try:
                launch.win_handle = winnet.run_as_admin(cmd[0], subprocess.list2cmdline(cmd[1:]))
            except PermissionError:
                raise HelperError("没有获得管理员授权（在 Windows 的确认框里选了“否”）")
            except OSError as e:
                raise HelperError("以管理员身份启动网络助手失败：%s" % e)
            return launch
        raise HelperError("不支持的操作系统：%s" % sys.platform)

    # ------------------------------------------------------------------ 读消息

    def _read_messages(self, reader: BinaryIO, eof: threading.Event) -> None:
        try:
            for raw in self._iter_lines(reader):
                self._handle_message(raw)
        finally:
            eof.set()
            # 助手退出：唤醒所有等待中的调用
            for q in list(self._responses.values()):
                q.put({"ok": False, "error": "网络助手已退出"})

    @staticmethod
    def _iter_lines(reader: BinaryIO) -> Iterator[bytes]:
        try:
            for raw in reader:
                yield raw
        except (OSError, ValueError):
            return

    def _handle_message(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return
        kind = msg.get("type")
        if kind == "ready":
            if msg.get("protocol") != HELPER_PROTOCOL:
                self.on_log("网络助手版本不匹配", "warning")
            self._ready.set()
        elif kind == "response":
            q = self._responses.get(msg.get("id"))
            if q is not None:
                q.put(msg)
        elif kind == "event":
            try:
                self.on_event(msg)
            except Exception as e:  # noqa: BLE001
                self.on_log("处理助手事件出错：%s" % e, "error")
        elif kind == "log":
            self.on_log(msg.get("msg", ""), msg.get("level", "info"))

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr.append(line)
                self.on_log("[助手] " + line, "debug")

    # ------------------------------------------------------------------ 调用

    def _write(self, data: bytes) -> None:
        if self.conn is not None:
            self.conn.sendall(data)
        else:
            assert self.proc and self.proc.stdin
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def call(self, cmd: str, timeout: float = 60, **params: Any) -> Dict[str, Any]:
        if not self.running:
            raise HelperError("网络助手没有运行")
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            q: "queue.Queue[Dict]" = queue.Queue()
            self._responses[rid] = q
            data = json.dumps(dict(params, id=rid, cmd=cmd), ensure_ascii=False) + "\n"
            try:
                self._write(data.encode("utf-8"))
            except (BrokenPipeError, OSError):
                self._responses.pop(rid, None)
                raise HelperError("网络助手已退出")
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            raise HelperError("网络助手响应超时：%s" % cmd)
        finally:
            self._responses.pop(rid, None)
        if not msg.get("ok"):
            raise HelperError(msg.get("error") or "未知错误")
        return msg.get("result") or {}

    # ------------------------------------------------------------------ 停止

    def stop(self) -> None:
        if self.conn is not None:
            self._stop_connection()
        elif self.proc is not None:
            self._stop_pipe()

    def _stop_connection(self) -> None:
        conn, self.conn = self.conn, None
        assert conn is not None
        try:
            try:
                conn.sendall(b'{"id": 0, "cmd": "shutdown"}\n')
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            # 助手恢复网卡后才断开连接
            self._eof.wait(15)
        finally:
            if self._reader is not None:
                try:
                    self._reader.close()
                except OSError:
                    pass
                self._reader = None
            conn.close()
            self._eof.set()
            if self._launch:
                self._launch.close()
                self._launch = None

    def _stop_pipe(self) -> None:
        assert self.proc is not None
        try:
            if self.proc.poll() is None and self.proc.stdin:
                try:
                    self.proc.stdin.write(b'{"id": 0, "cmd": "shutdown"}\n')
                    self.proc.stdin.flush()
                    self.proc.stdin.close()
                except OSError:
                    pass
                try:
                    self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    # 助手以 root 运行时普通用户无权发信号，只能靠关闭 stdin 让它自己退出
                    try:
                        self.proc.terminate()
                        self.proc.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        finally:
            self.proc = None
