"""Live view: read one confirmed, verified DID at its collection interval.

Values are decoded and shown, never recorded. The read is the DID's normal
read request (22 DID or 21 LL) and the session is the endpoint's reviewed
recipe; the worker allows nothing else. Reading stops on request, or after
several failed reads in a row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable

from ..admission import endpoint_session_row
from ..collector import JsonlWorkerClient, StoppedBeforeReady
from ..decoding import evaluate_formula, extract_raw
from ..i18n import _, ngettext
from ..identity import SignalRef, read_request_hex, response_header_bytes
from ..project import worker_executable
from ..repository import ProjectRepository
from ..worker_protocol import build_watch_config, write_worker_config
from .inventory import VERIFIED, InventoryTree
from .live import scope_hash

# Reads in a row that may fail before the view stops by itself.
FAILURE_LIMIT = 3

WorkerFactory = Callable[[Path, Path], Any]


@dataclass(frozen=True)
class WatchScope:
    endpoint_id: str
    acquisition_id: str
    data_id: str
    request_hex: str
    interval_ms: int
    signals: tuple[dict[str, Any], ...]
    session: dict[str, Any] = field(repr=False)
    profile_id: str = ""

    @property
    def digest(self) -> str:
        return scope_hash({
            "acquisition_id": self.acquisition_id, "request_hex": self.request_hex,
            "interval_ms": self.interval_ms, "session": self.session,
        })

    def summary(self) -> str:
        return _(
            "Read {did} on {endpoint} every {interval} ms and show {count} signal(s); nothing is recorded"
        ).format(did=self.data_id, endpoint=self.endpoint_id, interval=self.interval_ms, count=len(self.signals))

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id, "acquisition_id": self.acquisition_id,
            "request_hex": self.request_hex, "interval_ms": self.interval_ms,
            "session_steps": [step["request_hex"] for step in self.session["session_setup"]],
            "signals": [row["signal_ref"] for row in self.signals], "digest": self.digest,
        }


def build_watch_scope(repository: ProjectRepository, tree: InventoryTree, did_key: str) -> WatchScope:
    """The exact live view of one DID, or a ValueError saying what is missing."""

    found = next(
        ((endpoint, did) for endpoint in tree.endpoints for did in endpoint.dids if did.key == did_key), None,
    )
    if found is None:
        raise ValueError(_("no DID {did} in the candidate list").format(did=did_key))
    endpoint, did = found
    if not endpoint.confirmed or endpoint.endpoint_id is None:
        raise ValueError(_("confirm the endpoint first"))
    if did.state != VERIFIED:
        raise ValueError(_("verify this DID first; only DIDs that answered are watched"))
    settings = did.settings or {}
    interval = settings.get("target_interval_ms")
    if not interval:
        raise ValueError(_("set how often this DID is read first"))
    floor = int(repository.transport["safety"]["request_start_floor_ms"])
    if interval < floor:
        raise ValueError(_("the DID's interval {interval} ms is below the {floor} ms request floor").format(
            interval=interval, floor=floor,
        ))
    if repository.local_settings is None:
        raise ValueError(_("configure the connector first"))
    if not did.signals:
        raise ValueError(_("this DID has no signals to show"))
    data_id = int(did.data_id, 16)
    acquisition_id = f"{endpoint.endpoint_id}/0x{data_id:04X}"
    signals = tuple(
        {
            "signal_ref": signal.signal_ref, "name": signal.name, "unit": signal.unit,
            "formula": signal.formula, "states": dict(signal.states),
            "first_bit": SignalRef.parse(signal.signal_ref).first_bit,
            "last_bit": SignalRef.parse(signal.signal_ref).last_bit,
        }
        for signal in did.signals
    )
    return WatchScope(
        endpoint.endpoint_id, acquisition_id, f"0x{data_id:04X}", read_request_hex(data_id), int(interval),
        signals, endpoint_session_row(repository.transport, endpoint.endpoint_id), repository.profile_id,
    )


def run_watch(
    repository: ProjectRepository,
    scope: WatchScope,
    *,
    on_event: Callable[[dict[str, Any]], None],
    stop_event: threading.Event,
    worker_factory: WorkerFactory | None = None,
) -> str:
    """Read until stopped; returns why reading ended."""

    backend = repository.local_settings["backend"]
    executable = worker_executable(repository.root, backend)
    if not executable.is_file():
        raise FileNotFoundError(executable)
    acquisition = {
        "endpoint_id": scope.endpoint_id, "acquisition_id": scope.acquisition_id,
        "request_hex": scope.request_hex, "data_id": scope.data_id,
    }
    config = build_watch_config(scope.profile_id, scope.session, acquisition, backend)
    repository.paths.plans.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".watch-", dir=repository.paths.plans) as directory:
        path = Path(directory) / "config.json"
        write_worker_config(path, config)
        worker = (worker_factory or JsonlWorkerClient)(executable, path)
        try:
            try:
                worker.start()
            except StoppedBeforeReady:
                return "stopped"
            on_event({"event": "started", "interval_ms": scope.interval_ms})
            sequence = 0
            failures = 0
            header = response_header_bytes(int(scope.data_id, 16))
            while not stop_event.is_set():
                started = time.monotonic()
                result = worker.exchange(sequence, scope.acquisition_id, lambda _event: None)
                sequence += 1
                positive = result["outcome"] == "positive" and result.get("response_hex")
                values = _decode(scope, bytes.fromhex(result["response_hex"])[header:]) if positive else []
                on_event({
                    "event": "read", "sequence": sequence, "outcome": result["outcome"],
                    "nrc": result.get("nrc"), "latency_ms": result.get("latency_ms"), "values": values,
                })
                failures = 0 if positive else failures + 1
                if failures >= FAILURE_LIMIT:
                    return ngettext(
                        "stopped after {count} failed read in a row", "stopped after {count} failed reads in a row",
                        FAILURE_LIMIT,
                    ).format(count=FAILURE_LIMIT)
                remaining = scope.interval_ms / 1000 - (time.monotonic() - started)
                if remaining > 0:
                    stop_event.wait(remaining)
            return "stopped"
        finally:
            worker.close()


def _decode(scope: WatchScope, data: bytes) -> list[dict[str, Any]]:
    values = []
    for signal in scope.signals:
        row: dict[str, Any] = {"signal_ref": signal["signal_ref"], "raw": None, "value": None, "text": None}
        if signal["last_bit"] >= len(data) * 8:
            row["text"] = _("beyond the reply")
            values.append(row)
            continue
        raw = extract_raw(data, signal["first_bit"], signal["last_bit"])
        row["raw"] = raw
        row["text"] = signal["states"].get(str(raw))
        try:
            value = evaluate_formula(signal["formula"] or "raw", raw)
            row["value"] = value if math.isfinite(value) else None
        except (TypeError, ValueError, ZeroDivisionError):
            row["value"] = None
        values.append(row)
    return values
