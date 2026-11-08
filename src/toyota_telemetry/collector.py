"""Python-owned single-endpoint collection orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from .decoding import evaluate_formula, extract_raw
from .capture_writer import CaptureLimitReached, NativeRunWriter
from .capture_store import canonical_document_bytes, capture_set_hash
from .identity import AcquisitionRef, SignalRef, response_header_bytes
from .live_progress import LiveTargetLedger
from .objectives import ReplayObservation, evaluate_condition
from .scheduler import DeterministicScheduler
from .worker_protocol import exchange_command, parse_event, stop_command


PUBLIC_EVENT_SCHEMA = 1
PUBLIC_EVENT_FIELDS = {
    "scope": {
        "event", "schema_version", "run_id", "endpoint_id", "backend_kind",
        "capture_root", "plan_sha256",
    },
    "started": {"event", "schema_version", "run_id", "backend_kind"},
    "heartbeat": {
        "event", "schema_version", "run_id", "state", "current_acquisition_id",
    },
    "response_pending": {
        "event", "schema_version", "run_id", "pending_count", "pending_duration_ms",
    },
    "progress": {
        "event", "schema_version", "run_id", "elapsed_ms", "exchanges", "failed_exchanges",
        "targets", "targets_complete",
    },
    "exchange": {"event", "schema_version", "run_id", "acquisition_id", "outcome"},
    "segment_rotated": {"event", "schema_version", "run_id", "segment_index"},
    "finished": {"event", "schema_version", "run_id", "reason"},
    "error": {"event", "schema_version", "run_id", "detail"},
}


class CollectionCrashed(RuntimeError):
    """The worker or collector failed without a clean capture finalization."""


class StoppedBeforeReady(Exception):
    """The operator stopped the run while the worker was still starting."""


class TransactionWorker(Protocol):
    def start(self) -> Mapping[str, Any]: ...
    def exchange(
        self,
        sequence: int,
        transaction_id: str,
        event_sink: Callable[[Mapping[str, Any]], None],
    ) -> Mapping[str, Any]: ...
    def request_stop(self) -> None: ...
    def close(self) -> None: ...
    def terminate(self) -> None: ...


class JsonlWorkerClient:
    """Blocking client for one private C# adapter worker subprocess."""

    def __init__(self, executable: Path, config_path: Path) -> None:
        self.command = [str(executable), "--worker", "--config", str(config_path)]
        self.process: subprocess.Popen[str] | None = None
        self._write_lock = threading.Lock()
        self._stop_sent = False

    def start(self) -> Mapping[str, Any]:
        if self.process is not None:
            raise RuntimeError("worker is already started")
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        event = self._read_event()
        if event["event"] == "stopped" and self._stop_sent:
            raise StoppedBeforeReady()
        if event["event"] != "ready":
            raise CollectionCrashed(f"worker emitted {event['event']} before ready")
        return event

    def exchange(
        self,
        sequence: int,
        transaction_id: str,
        event_sink: Callable[[Mapping[str, Any]], None],
    ) -> Mapping[str, Any]:
        self._write(exchange_command(sequence, transaction_id))
        while True:
            event = self._read_event()
            if event["event"] == "response_pending":
                if event["sequence"] != sequence:
                    raise CollectionCrashed("worker pending sequence mismatch")
                event_sink(event)
                continue
            if event["event"] != "result" or event["sequence"] != sequence:
                raise CollectionCrashed("worker result sequence mismatch")
            return event

    def request_stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            with self._write_lock:
                if self._stop_sent:
                    return
                self._stop_sent = True
                self._write_unlocked(stop_command())

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                self.request_stop()
            except CollectionCrashed:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.terminate()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def terminate(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait()

    def _write(self, line: str) -> None:
        if self.process is None or self.process.stdin is None or self.process.poll() is not None:
            raise CollectionCrashed("worker is not running")
        with self._write_lock:
            self._write_unlocked(line)

    def _write_unlocked(self, line: str) -> None:
        assert self.process is not None and self.process.stdin is not None
        try:
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CollectionCrashed("worker input closed unexpectedly") from exc

    def _read_event(self) -> Mapping[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise CollectionCrashed("worker is not started")
        line = self.process.stdout.readline()
        if line:
            return parse_event(line)
        detail = ""
        if self.process.stderr is not None:
            detail = self.process.stderr.read().strip()
        raise CollectionCrashed("worker output closed unexpectedly" + (f": {detail}" if detail else ""))


class CollectionRunner:
    """Join the deterministic scheduler, transaction worker, and capture writer."""

    def __init__(
        self,
        plan: Mapping[str, Any],
        writer: NativeRunWriter,
        worker: TransactionWorker,
        *,
        event_sink: Callable[[Mapping[str, Any]], None],
        stop_event: threading.Event,
        monotonic_ms: Callable[[], int] | None = None,
        wait_ms: Callable[[int], None] | None = None,
        utc_now: Callable[[], str] | None = None,
        heartbeat_ms: int = 1000,
        max_duration_ms: int | None = None,
    ) -> None:
        self.plan = dict(plan)
        self.writer = writer
        self.worker = worker
        self.event_sink = event_sink
        self.stop_event = stop_event
        self.monotonic_ms = monotonic_ms or (lambda: time.monotonic_ns() // 1_000_000)
        self.wait_ms = wait_ms or (lambda delay: self.stop_event.wait(delay / 1000))
        self.utc_now = utc_now or (
            lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        if heartbeat_ms < 1:
            raise ValueError("heartbeat interval must be positive")
        self.heartbeat_ms = heartbeat_ms
        # A time slice: when it ends, the run finishes as "slice_end" so a
        # rotation can hand over to the next endpoint.
        self.max_duration_ms = max_duration_ms
        sessions = self.plan.get("endpoint_sessions", [])
        selected_sessions = [
            session for session in sessions
            if session["endpoint_id"] == self.writer.endpoint_id
        ]
        if len(selected_sessions) != 1:
            raise ValueError("collector requires the writer's one admitted endpoint")
        self.session = selected_sessions[0]
        self.acquisitions = {
            row["acquisition_id"]: row
            for row in self.plan["acquisitions"]
            if row["endpoint_id"] == self.writer.endpoint_id
        }
        self.scheduler = DeterministicScheduler(
            self.acquisitions.values(),
            start_ms=0,
            request_start_floor_ms=self.session["request_start_floor_ms"],
        )
        # A run collects one endpoint: it completes when that endpoint's own
        # targets are complete. Targets elsewhere whose condition reads one of
        # this endpoint's DIDs are tracked only to keep that DID polled.
        self.own_targets = sorted({
            key for row in self.acquisitions.values() for key in row["objective_ids"]
        })
        tracked = set(self.own_targets) | {
            key for row in self.acquisitions.values() for key in row.get("dependency_for", [])
        }
        self.ledger = LiveTargetLedger(
            target for target in self.plan["objectives"] if target["signal_ref"] in tracked
        )
        self._apply_progress()
        self._state_lock = threading.Lock()
        self._state = "disconnected"
        self._current_acquisition: str | None = None
        self._done = threading.Event()
        self._started_ms = 0
        self._worker_sequence = 0
        self._exchanges = 0
        self._failed_exchanges = 0
        self._target_summary = self.ledger.summary(self.own_targets)
        self._context: dict[str, ReplayObservation] = {}

    def run(self) -> str:
        self._started_ms = self.monotonic_ms()
        heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        stop_relay = threading.Thread(target=self._stop_relay_loop, daemon=True)
        heartbeat.start()
        stop_relay.start()
        try:
            ready = self.worker.start()
            if ready["endpoint_id"] != self.writer.endpoint_id:
                raise CollectionCrashed("worker ready endpoint mismatch")
            self._set_state("connected")
            self._emit("started", backend_kind=ready["backend_kind"])
            reason = self._loop()
            self.worker.close()
            self._set_state("disconnected")
            self._emit_progress()
            self._emit("finished", reason=reason)
            return reason
        except StoppedBeforeReady:
            # Stopped while the adapter or session was still starting: an
            # empty run that ended normally, not a crash.
            self.writer.finish("operator_stop")
            self.worker.close()
            self._set_state("disconnected")
            self._emit_progress()
            self._emit("finished", reason="operator_stop")
            return "operator_stop"
        except CaptureLimitReached:
            self.worker.request_stop()
            self.worker.close()
            self._set_state("disconnected")
            self._emit_progress()
            self._emit("finished", reason="size_limit")
            return "size_limit"
        except KeyboardInterrupt:
            self.stop_event.set()
            self.worker.request_stop()
            self.writer.finish("operator_stop")
            self.worker.close()
            self._set_state("disconnected")
            self._emit_progress()
            self._emit("finished", reason="operator_stop")
            return "operator_stop"
        except Exception as exc:
            self._set_state("transport-error")
            self._emit("error", detail=str(exc))
            self.worker.terminate()
            self.writer.crash()
            raise CollectionCrashed(str(exc)) from exc
        finally:
            self._done.set()
            heartbeat.join(timeout=max(1, self.heartbeat_ms * 2) / 1000)
            stop_relay.join(timeout=0.2)

    def _loop(self) -> str:
        while True:
            if self.stop_event.is_set():
                self.writer.finish("operator_stop")
                return "operator_stop"
            now = self.monotonic_ms() - self._started_ms
            if self.max_duration_ms is not None and now >= self.max_duration_ms:
                self.writer.finish("slice_end")
                return "slice_end"
            if self.writer.needs_rotation(now):
                self.writer.rotate(now)
                self._emit("segment_rotated", segment_index=self.writer.segment_index)
            decision = self.scheduler.select(now)
            if decision is None:
                due = self.scheduler.next_due_ms()
                delay = 10 if due is None else max(1, min(50, due - now))
                self.wait_ms(delay)
                continue
            acquisition = self.acquisitions[decision.acquisition_id]
            with self._state_lock:
                self._current_acquisition = decision.acquisition_id
            result, retry_count = self._exchange_with_retries(acquisition)
            completed = self.monotonic_ms() - self._started_ms
            outcome = result["outcome"]
            observations = self._decode(acquisition, result, completed)
            self.writer.append_exchange({
                "acquisition_id": decision.acquisition_id,
                "endpoint_id": acquisition["endpoint_id"],
                "data_id": acquisition["data_id"],
                "response_utc": self.utc_now(),
                "request_elapsed_ms": decision.selected_at_ms,
                "response_elapsed_ms": completed,
                "request_hex": acquisition["request_hex"],
                "response_hex": result["response_hex"],
                "outcome": outcome,
                "negative_response_code": result["nrc"],
                "pending_count": result["pending_count"],
                "pending_duration_ms": result["pending_duration_ms"],
                "latency_ms": result["latency_ms"],
                "retry_count": retry_count,
                "scheduler_reason": decision.reason,
                "objective_ids": acquisition["objective_ids"],
                "observations": observations,
            })
            self.scheduler.complete(decision.acquisition_id, completed, outcome)
            self._apply_progress()
            summary = self.ledger.summary(self.own_targets)
            with self._state_lock:
                self._current_acquisition = None
                self._target_summary = summary
                self._exchanges += 1
                if outcome != "positive":
                    self._failed_exchanges += 1
            self._set_state("connected")
            self._emit("exchange", acquisition_id=decision.acquisition_id, outcome=outcome)
            if self.ledger.all_complete(self.own_targets):
                self.writer.finish("plan_complete")
                return "plan_complete"
            if outcome == "transport_error":
                self.writer.finish("transport_lost")
                return "transport_lost"
            if outcome == "abandoned" or self.stop_event.is_set():
                self.writer.finish("operator_stop")
                return "operator_stop"

    def _exchange_with_retries(
        self, acquisition: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], int]:
        retries = int(self.session["transport_retries"])
        for attempt in range(retries + 1):
            self._worker_sequence += 1
            result = self.worker.exchange(
                self._worker_sequence,
                acquisition["acquisition_id"],
                self._worker_event,
            )
            if result["outcome"] != "timeout" or attempt == retries or self.stop_event.is_set():
                return result, attempt
            self.wait_ms(int(self.session["request_start_floor_ms"]))
        raise AssertionError("retry loop did not return")

    def _worker_event(self, event: Mapping[str, Any]) -> None:
        if event["event"] == "response_pending":
            self._set_state("response-pending")
            self._emit(
                "response_pending",
                pending_count=event["pending_count"],
                pending_duration_ms=event["pending_duration_ms"],
            )

    def _decode(
        self,
        acquisition: Mapping[str, Any],
        result: Mapping[str, Any],
        elapsed_ms: int,
    ) -> list[dict[str, Any]]:
        if result["outcome"] != "positive" or result["response_hex"] is None:
            return []
        payload = bytes.fromhex(result["response_hex"])
        data = payload[response_header_bytes(int(acquisition["data_id"], 16)):]
        decoded: list[tuple[dict[str, Any], int, float | None]] = []
        acquisition_ref = AcquisitionRef.parse(acquisition["acquisition_id"])
        for row in self.plan["decodes"]:
            ref = SignalRef.parse(row["signal_ref"])
            if row["acquisition_id"] != str(acquisition_ref):
                continue
            raw = extract_raw(data, row["first_bit"], row["last_bit"])
            try:
                value = evaluate_formula(row["formula"], raw)
                if not math.isfinite(value):
                    value = None
            except (TypeError, ValueError, ZeroDivisionError):
                value = None
            decoded.append((row, raw, value))
            self._context[row["signal_ref"]] = ReplayObservation(
                self.writer.run_id,
                self.writer.run_sequence + 1,
                elapsed_ms,
                None,
                row["signal_ref"],
                raw,
                value,
            )
        output = []
        condition_by_signal = {}
        for target in self.plan["objectives"]:
            signal_ref = target["signal_ref"]
            if signal_ref not in self.ledger.states:
                continue
            condition_by_signal[signal_ref] = (
                "true" if target["condition"] is None else evaluate_condition(
                    target["condition"], self._context, self.writer.run_id, elapsed_ms
                )
            )
            self.ledger.set_condition(signal_ref, condition_by_signal[signal_ref])
        for row, raw, value in decoded:
            applicability = condition_by_signal.get(row["signal_ref"], "true")
            self.ledger.observe(row["signal_ref"], raw, value, applicability, elapsed_ms)
            width = (row["last_bit"] - row["first_bit"] + 8) // 8
            output.append({
                "signal_ref": row["signal_ref"],
                "raw_hex": raw.to_bytes(width, "big").hex(" ").upper(),
                "decoded_value": value,
                "applicability": applicability,
            })
        return output

    def _apply_progress(self) -> None:
        for acquisition_id, acquisition in self.acquisitions.items():
            progress = self.ledger.acquisition_progress(acquisition)
            self.scheduler.update_acquisition(
                acquisition_id,
                active=progress.active,
                background=progress.background,
                deficit=progress.deficit,
                opportunity=progress.opportunity,
            )

    def _stop_relay_loop(self) -> None:
        while not self._done.wait(0.05):
            if self.stop_event.is_set():
                try:
                    self.worker.request_stop()
                except Exception:
                    pass
                return

    def _heartbeat_loop(self) -> None:
        while not self._done.wait(self.heartbeat_ms / 1000):
            with self._state_lock:
                state = self._state
                current = self._current_acquisition
            self._emit("heartbeat", state=state, current_acquisition_id=current)
            self._emit_progress()

    def _emit_progress(self) -> None:
        with self._state_lock:
            exchanges = self._exchanges
            failed = self._failed_exchanges
            summary = dict(self._target_summary)
        self._emit(
            "progress",
            elapsed_ms=max(0, self.monotonic_ms() - self._started_ms),
            exchanges=exchanges,
            failed_exchanges=failed,
            targets=summary["targets"],
            targets_complete=summary["complete"],
        )

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self._state = state

    def _emit(self, event: str, **fields: Any) -> None:
        self.event_sink(_validate_public_event({
            "event": event,
            "schema_version": PUBLIC_EVENT_SCHEMA,
            "run_id": self.writer.run_id,
            **fields,
        }))


def load_verified_plan(path: Path, repository: Any) -> tuple[dict[str, Any], bytes]:
    """Validate exact admitted bytes and current project sources before live work."""

    data = path.read_bytes()
    try:
        plan = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid admitted plan: {path}") from exc
    if not isinstance(plan, dict) or canonical_document_bytes(plan) != data:
        raise ValueError("admitted plan is not canonical exact-schema JSON")
    required = {
        "document_type", "schema_version", "project_id", "profile_id", "created_utc",
        "source_hashes", "admitted", "live_authorized", "admission", "stress", "storage",
        "endpoint_sessions", "acquisitions", "objectives", "decodes", "content_sha256",
    }
    if set(plan) != required:
        raise ValueError("admitted plan fields do not match schema 1")
    if (
        plan["document_type"] != "telemetry.admitted-plan"
        or plan["schema_version"] != 1
        or plan["project_id"] != repository.project_id
        or plan["profile_id"] != repository.profile_id
        or plan["admitted"] is not True
        or plan["live_authorized"] is not True
    ):
        raise ValueError("admitted plan identity or authorization mismatch")
    unsigned = dict(plan)
    digest = unsigned.pop("content_sha256")
    if hashlib.sha256(canonical_document_bytes(unsigned)).hexdigest() != digest:
        raise ValueError("admitted plan content hash mismatch")
    if plan["source_hashes"] != repository.cached_source_hashes():
        raise ValueError("admitted plan does not match the project cache sources")
    current = {
        "transport": hashlib.sha256(repository.paths.transport.read_bytes()).hexdigest(),
        "collection_policy": hashlib.sha256(repository.paths.collection_policy.read_bytes()).hexdigest(),
        "signal_review": hashlib.sha256(repository.paths.signal_review.read_bytes()).hexdigest(),
        "catalog_manifest": hashlib.sha256(repository.paths.catalog_manifest.read_bytes()).hexdigest(),
        "capture_set": capture_set_hash(repository.paths.captures),
    }
    for kind, digest in current.items():
        if plan["source_hashes"].get(kind) != digest:
            raise ValueError(f"admitted plan source is stale: {kind}")
    return plan, data


def parse_public_event(line: str) -> dict[str, Any]:
    """Validate the exact public JSONL event contract."""

    try:
        event = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError("collector emitted invalid JSON") from exc
    return _validate_public_event(event)


def _validate_public_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict) or event.get("event") not in PUBLIC_EVENT_FIELDS:
        raise ValueError("collector event type is invalid")
    event_type = event["event"]
    if set(event) != PUBLIC_EVENT_FIELDS[event_type]:
        raise ValueError(f"collector {event_type} event fields mismatch")
    if event["schema_version"] != PUBLIC_EVENT_SCHEMA:
        raise ValueError("collector event schema mismatch")
    if event["run_id"] is not None and not isinstance(event["run_id"], str):
        raise ValueError("collector event run_id is invalid")
    if event_type == "heartbeat" and event["state"] not in {
        "disconnected", "connected", "response-pending", "transport-error"
    }:
        raise ValueError("collector heartbeat state is invalid")
    return event
