"""把后台线程里运行的流程接到界面上：信号转发 + 阻塞式询问。"""

import threading
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, QThread, Signal

from ..bundle.builder import build_bundle
from ..core.workflow import Credentials, Ui, Workflow


class Request:
    """后台线程向界面线程发起的询问，界面回答后唤醒后台线程。"""

    def __init__(self, kind: str, **payload: Any):
        self.kind = kind
        self.payload = payload
        self.result: Any = None
        self.event = threading.Event()

    def answer(self, result: Any) -> None:
        self.result = result
        self.event.set()


class GuiUi(QObject, Ui):
    log_signal = Signal(str, str)
    step_signal = Signal(str, str, str)
    device_signal = Signal(object)
    progress_signal = Signal(str, float)
    request_signal = Signal(object)

    def __init__(self) -> None:
        QObject.__init__(self)
        self._cancel = threading.Event()

    # Ui 接口 ---------------------------------------------------------------

    def log(self, msg: str, level: str = "info") -> None:
        self.log_signal.emit(msg, level)

    def step(self, step_id: str, state: str, detail: str = "") -> None:
        self.step_signal.emit(step_id, state, detail)

    def device(self, info: Dict) -> None:
        self.device_signal.emit(info)

    def progress(self, text: str, fraction: float) -> None:
        self.progress_signal.emit(text, fraction)

    def _ask(self, req: Request) -> Any:
        self.request_signal.emit(req)
        while not req.event.wait(0.2):
            if self._cancel.is_set():
                return None
        return req.result

    def ask_credentials(self, title: str, username: str, error: str = "") -> Optional[Credentials]:
        return self._ask(Request("credentials", title=title, username=username, error=error))

    def confirm(self, title: str, text: str, default: bool = True) -> bool:
        r = self._ask(Request("confirm", title=title, text=text, default=default))
        return bool(r) if r is not None else False

    def choose(self, title: str, options: List[str]) -> Optional[int]:
        return self._ask(Request("choose", title=title, options=options))

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    # 控制 -----------------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    def reset(self) -> None:
        self._cancel.clear()


class WorkflowRunner(QThread):
    finished_with = Signal(object)

    def __init__(self, workflow: Workflow, mode: str, action: str = "run"):
        super().__init__()
        self.workflow = workflow
        self.mode = mode
        self.action = action

    def run(self) -> None:
        try:
            if self.action == "restore":
                self.workflow.restore_network()
                result: Dict = {"ok": True, "restored": True}
            else:
                result = self.workflow.run(self.mode)
        except Exception as e:  # noqa: BLE001 - 任何异常都要回到界面
            result = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        self.finished_with.emit(result)


class BundleBuildRunner(QThread):
    log_signal = Signal(str)
    progress_signal = Signal(str, float)
    finished_with = Signal(object)

    def __init__(self, deb: str, output: str, mirror: str, releases: List[str]):
        super().__init__()
        self.deb = deb
        self.output = output
        self.mirror = mirror
        self.releases = releases

    def run(self) -> None:
        try:
            path = build_bundle(
                self.deb, self.output, releases=self.releases, mirror=self.mirror,
                log=self.log_signal.emit,
                progress=lambda stage, frac: self.progress_signal.emit(stage, frac),
            )
            self.finished_with.emit({"ok": True, "path": path})
        except Exception as e:  # noqa: BLE001
            self.finished_with.emit({"ok": False, "error": str(e)})
