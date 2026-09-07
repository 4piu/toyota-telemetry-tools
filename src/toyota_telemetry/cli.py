"""Unified telemetry command dispatcher."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import uuid

from .capture_writer import NativeRunWriter
from .collector import (
    CollectionCrashed,
    CollectionRunner,
    JsonlWorkerClient,
    load_verified_plan,
    parse_public_event,
)
from .project import find_project_root
from .repository import ProjectRepository
from .worker_protocol import build_worker_config, write_worker_config


def main() -> int:
    parser = argparse.ArgumentParser(prog="telemetry")
    subcommands = parser.add_subparsers(dest="command", required=True)
    collect = subcommands.add_parser("collect", help="run an admitted live collection plan")
    collect.add_argument("--project-root", type=Path)
    collect.add_argument("--plan", type=Path)
    collect.add_argument("--endpoint")
    collect.add_argument("--note")
    collect.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    return _collect(args)


def _collect(args: argparse.Namespace) -> int:
    stop = threading.Event()
    output_lock = threading.Lock()

    def emit(event: dict) -> None:
        parse_public_event(json.dumps(event, separators=(",", ":")))
        with output_lock:
            if args.as_json:
                print(json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)
            elif event["event"] in {"scope", "started", "finished", "error"}:
                print(_human_event(event), flush=True)

    try:
        root = args.project_root.resolve() if args.project_root else find_project_root()
        repository = ProjectRepository(root)
        if repository.local_settings is None:
            raise ValueError("local settings are required for live collection")
        plan_path = (
            args.plan.resolve()
            if args.plan and args.plan.is_absolute()
            else (root / args.plan).resolve()
            if args.plan
            else repository.paths.plans / "admitted-plan.json"
        )
        plan, plan_bytes = load_verified_plan(plan_path, repository)
        endpoint_ids = [row["endpoint_id"] for row in plan["endpoint_sessions"]]
        endpoint_id = args.endpoint
        if endpoint_id is None:
            if len(endpoint_ids) != 1:
                raise ValueError("--endpoint is required for a multi-endpoint admitted plan")
            endpoint_id = endpoint_ids[0]
        if endpoint_id not in endpoint_ids:
            raise ValueError(f"endpoint is absent from admitted plan: {endpoint_id}")
        backend = repository.local_settings["backend"]
        executable = Path(backend["executable"])
        if not executable.is_absolute():
            executable = (root / executable).resolve()
        if not executable.is_file():
            raise FileNotFoundError(executable)
        created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_id = str(uuid.uuid4())
        emit({
            "event": "scope", "schema_version": 1, "run_id": run_id,
            "endpoint_id": endpoint_id, "backend_kind": backend["kind"],
            "capture_root": repository.paths.captures.relative_to(root).as_posix(),
            "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        })
        worker_config = build_worker_config(plan, endpoint_id, backend)
        with tempfile.TemporaryDirectory(prefix=".worker-", dir=repository.paths.plans) as directory:
            worker_config_path = Path(directory) / "config.json"
            write_worker_config(
                worker_config_path,
                worker_config,
            )
            writer = NativeRunWriter(
                repository.paths.captures,
                plan,
                plan_bytes,
                run_id=run_id,
                created_utc=created,
                backend_kind=backend["kind"],
                endpoint_id=endpoint_id,
                run_note=args.note,
            )
            worker = JsonlWorkerClient(executable, worker_config_path)
            control_errors = _start_public_control(stop)
            try:
                runner = CollectionRunner(
                    plan,
                    writer,
                    worker,
                    event_sink=emit,
                    stop_event=stop,
                )
            except Exception:
                writer.crash()
                raise
            reason = runner.run()
            if control_errors:
                raise ValueError(control_errors[0])
        return 0 if reason in {"operator_stop", "plan_complete", "size_limit"} else 1
    except (CollectionCrashed, FileNotFoundError, OSError, ValueError) as exc:
        emit({"event": "error", "schema_version": 1, "run_id": None, "detail": str(exc)})
        return 1


def _start_public_control(stop: threading.Event) -> list[str]:
    errors: list[str] = []

    def read() -> None:
        line = sys.stdin.readline()
        if line == "":
            return
        if line.rstrip("\r\n") != "stop":
            errors.append("telemetry collect stdin accepts exactly the 'stop' verb")
            stop.set()
            return
        stop.set()

    threading.Thread(target=read, name="telemetry-collect-stdin", daemon=True).start()
    return errors


def _human_event(event: dict) -> str:
    if event["event"] == "scope":
        return (
            f"LIVE collection: adapter={event['backend_kind']} endpoint={event['endpoint_id']} "
            f"output={event['capture_root']} plan={event['plan_sha256']}"
        )
    if event["event"] == "started":
        return f"Collection started: run {event['run_id']}"
    if event["event"] == "finished":
        return f"Collection finished: {event['reason']}"
    return f"ERROR: {event['detail']}"


if __name__ == "__main__":
    raise SystemExit(main())
