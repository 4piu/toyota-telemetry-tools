"""Run one blocking service call off the UI thread and relay its events."""

from __future__ import annotations

from collections.abc import Callable
import threading
import traceback
from typing import Any

from PySide6.QtCore import QObject, Signal


class LiveJob(QObject):
    """A single background job with a cooperative stop event.

    ``work`` receives an event callback and the stop event. Events and the
    final result are delivered on the UI thread through queued signals.
    """

    event = Signal(object)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, work: Callable[[Callable[[dict], None], threading.Event], Any], parent=None) -> None:
        super().__init__(parent)
        self._work = work
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("job already started")
        self._thread = threading.Thread(target=self._run, name="telemetry-ui-job", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self.stop_event.set()

    def wait(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            result = self._work(self.event.emit, self.stop_event)
        except Exception as exc:  # reported to the user, never swallowed
            detail = str(exc) or exc.__class__.__name__
            self.failed.emit(f"{detail}\n\n{traceback.format_exc(limit=3)}")
        else:
            self.finished.emit(result)
