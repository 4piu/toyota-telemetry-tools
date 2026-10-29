"""Keep project work off the UI thread.

All service calls from the pages run on one background thread, so they never
overlap each other (the cache is rebuilt by whichever call needs it first).
Results return to the UI thread. A newer request with the same key makes older
ones stale: a stale request that has not started is skipped, and a stale result
is dropped.
"""

from __future__ import annotations

from collections.abc import Callable
import traceback
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class _Relay(QObject):
    done = Signal(int, object)
    failed = Signal(int, str)


class _Task(QRunnable):
    def __init__(self, ticket: int, work: Callable[[], Any], is_current: Callable[[], bool], relay: _Relay) -> None:
        super().__init__()
        self.ticket = ticket
        self.work = work
        self.is_current = is_current
        self.relay = relay

    @Slot()
    def run(self) -> None:
        if not self.is_current():
            self.relay.done.emit(self.ticket, _SKIPPED)
            return
        try:
            result = self.work()
        except Exception as exc:  # reported on the UI thread
            message = str(exc) or exc.__class__.__name__
            self.relay.failed.emit(self.ticket, f"{message}\n\n{traceback.format_exc(limit=4)}")
        else:
            self.relay.done.emit(self.ticket, result)


_SKIPPED = object()


class ServiceWorker(QObject):
    busy_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._relay = _Relay(self)
        self._relay.done.connect(self._deliver)
        self._relay.failed.connect(self._fail)
        self._tickets = 0
        self._latest: dict[str, int] = {}
        self._pending: dict[int, tuple[str, Callable[[Any], None], Callable[[str], None] | None]] = {}

    @property
    def idle(self) -> bool:
        return not self._pending

    def submit(
        self,
        key: str,
        work: Callable[[], Any],
        done: Callable[[Any], None],
        failed: Callable[[str], None] | None = None,
    ) -> int:
        self._tickets += 1
        ticket = self._tickets
        self._latest[key] = ticket
        was_idle = self.idle
        self._pending[ticket] = (key, done, failed)
        self._pool.start(_Task(ticket, work, lambda: self._latest.get(key) == ticket, self._relay))
        if was_idle:
            self.busy_changed.emit(True)
        return ticket

    def wait(self, timeout_ms: int = -1) -> bool:
        """Block until queued work has run (tests and shutdown only)."""

        return self._pool.waitForDone(timeout_ms)

    def _finish(self, ticket: int):
        entry = self._pending.pop(ticket, None)
        if self.idle:
            self.busy_changed.emit(False)
        if entry is None:
            return None
        key, done, failed = entry
        return (done, failed) if self._latest.get(key) == ticket else None

    @Slot(int, object)
    def _deliver(self, ticket: int, result: object) -> None:
        callbacks = self._finish(ticket)
        if callbacks is not None and result is not _SKIPPED:
            callbacks[0](result)

    @Slot(int, str)
    def _fail(self, ticket: int, detail: str) -> None:
        callbacks = self._finish(ticket)
        if callbacks is not None and callbacks[1] is not None:
            callbacks[1](detail)
