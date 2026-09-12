"""Latest-result-only background execution for Qt presenters."""

from __future__ import annotations

from collections.abc import Callable
import threading
import traceback
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from toyota_telemetry.operator import QueryCancelled


class _TaskSignals(QObject):
    result = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)
    finished = Signal(int)


class _Task(QRunnable):
    def __init__(
        self,
        generation: int,
        work: Callable[[threading.Event], Any],
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self.generation = generation
        self.work = work
        self.cancel_event = cancel_event
        self.signals = _TaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.work(self.cancel_event)
            if self.cancel_event.is_set():
                self.signals.cancelled.emit(self.generation)
            else:
                self.signals.result.emit(self.generation, result)
        except QueryCancelled:
            self.signals.cancelled.emit(self.generation)
        except Exception:
            self.signals.failed.emit(self.generation, traceback.format_exc())
        finally:
            self.signals.finished.emit(self.generation)


class LatestTaskController(QObject):
    """Run work in a thread pool and publish only the newest generation."""

    result = Signal(object)
    failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, pool: QThreadPool | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = pool or QThreadPool.globalInstance()
        self._generation = 0
        self._cancellations: dict[int, threading.Event] = {}
        self._busy = False

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def busy(self) -> bool:
        return self._busy

    def start(self, work: Callable[[threading.Event], Any]) -> int:
        self.cancel()
        self._generation += 1
        generation = self._generation
        cancel_event = threading.Event()
        self._cancellations[generation] = cancel_event
        task = _Task(generation, work, cancel_event)
        task.signals.result.connect(self._accept_result)
        task.signals.failed.connect(self._accept_failure)
        task.signals.finished.connect(self._finished)
        if not self._busy:
            self._busy = True
            self.busy_changed.emit(True)
        self._pool.start(task)
        return generation

    def cancel(self) -> None:
        for event in self._cancellations.values():
            event.set()

    @Slot(int, object)
    def _accept_result(self, generation: int, value: object) -> None:
        if generation == self._generation:
            self.result.emit(value)

    @Slot(int, str)
    def _accept_failure(self, generation: int, detail: str) -> None:
        if generation == self._generation:
            self.failed.emit(detail)

    @Slot(int)
    def _finished(self, generation: int) -> None:
        self._cancellations.pop(generation, None)
        if generation == self._generation and self._busy:
            self._busy = False
            self.busy_changed.emit(False)
