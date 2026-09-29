"""启动并调用以 root 运行的网络助手（Linux 上通过 pkexec 弹出系统授权框）。"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..helper import HELPER_PROTOCOL

EventFn = Callable[[Dict[str, Any]], None]
LogFn = Callable[[str, str], None]


class HelperError(Exception):
    pass


def helper_command() -> List[str]:
    """构造启动助手的命令。"""
    if getattr(sys, "frozen", False):
        base = [sys.executable, "helper"]
    else:
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "helper", "__main__.py")
        base = [os.path.abspath(sys.executable), script]
    if not sys.platform.startswith("linux"):
        raise HelperError("当前只支持 Ubuntu/Linux 作为电脑 A，Windows 和 macOS 正在开发中")
    if os.geteuid() == 0:
        return base
    if shutil.which("pkexec"):
        return ["pkexec"] + base
    if shutil.which("sudo") and sys.stdin.isatty():
        return ["sudo"] + base
    raise HelperError("需要管理员权限：请安装 policykit-1（pkexec），或用 sudo 运行本程序")


class HelperClient:
    def __init__(self, on_event: EventFn, on_log: LogFn):
        self.on_event = on_event
        self.on_log = on_log
        self.proc: Optional[subprocess.Popen] = None
        self._responses: Dict[int, "queue.Queue[Dict]"] = {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stderr: List[str] = []

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout: float = 180) -> None:
        if self.running:
            return
        cmd = helper_command()
        self.on_log("启动网络助手（需要管理员授权）：%s" % " ".join(os.path.basename(c) for c in cmd), "debug")
        self._ready.clear()
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
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

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
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
        # 助手退出：唤醒所有等待中的调用
        for q in list(self._responses.values()):
            q.put({"ok": False, "error": "网络助手已退出"})

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr.append(line)
                self.on_log("[助手] " + line, "debug")

    def call(self, cmd: str, timeout: float = 60, **params: Any) -> Dict[str, Any]:
        if not self.running:
            raise HelperError("网络助手没有运行")
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            q: "queue.Queue[Dict]" = queue.Queue()
            self._responses[rid] = q
            data = json.dumps(dict(params, id=rid, cmd=cmd), ensure_ascii=False) + "\n"
            assert self.proc and self.proc.stdin
            try:
                self.proc.stdin.write(data.encode("utf-8"))
                self.proc.stdin.flush()
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

    def stop(self) -> None:
        if not self.proc:
            return
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
