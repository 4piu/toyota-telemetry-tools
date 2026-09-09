"""Unified telemetry command dispatcher."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any
import uuid

from .capture_writer import NativeRunWriter
from .capture_store import inspect_capture_tree
from .collector import (
    CollectionCrashed,
    CollectionRunner,
    JsonlWorkerClient,
    load_verified_plan,
    parse_public_event,
)
from .project import find_project_root
from .repository import ProjectRepository
from .admission import compile_plan
from .exporting import export_project
from . import __version__
from .worker_protocol import build_worker_config, write_worker_config
from .tui import OperatorWorkspace


def main() -> int:
    parser = argparse.ArgumentParser(prog="telemetry")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    catalog = subcommands.add_parser("catalog", help="catalog cache operations")
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_import = catalog_commands.add_parser("import", help="import normalized catalog rows")
    catalog_import.add_argument("--project-root", type=Path)
    catalog_import.add_argument("--catalog-rows", type=Path, required=True)
    catalog_import.add_argument("--json", action="store_true", dest="as_json")
    catalog_import.set_defaults(handler=_catalog_import)

    cache = subcommands.add_parser("cache", help="derived project cache operations")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_rebuild = cache_commands.add_parser("rebuild", help="rebuild the derived project cache")
    cache_rebuild.add_argument("--project-root", type=Path)
    cache_rebuild.add_argument("--catalog-rows", type=Path, required=True)
    cache_rebuild.add_argument("--json", action="store_true", dest="as_json")
    cache_rebuild.set_defaults(handler=_cache_rebuild)

    plan_command = subcommands.add_parser("plan", help="compile and admit collection work offline")
    plan_command.add_argument("--project-root", type=Path)
    plan_command.add_argument("--output", type=Path)
    plan_command.add_argument("--json", action="store_true", dest="as_json")
    plan_command.set_defaults(handler=_plan)

    collect = subcommands.add_parser("collect", help="run an admitted live collection plan")
    collect.add_argument("--project-root", type=Path)
    collect.add_argument("--plan", type=Path)
    collect.add_argument("--endpoint")
    collect.add_argument("--note")
    collect.add_argument("--json", action="store_true", dest="as_json")
    collect.set_defaults(handler=_collect)
    workspace = subcommands.add_parser(
        "workspace", help="open the offline-first operator workspace"
    )
    workspace.add_argument("--project-root", type=Path)
    workspace.set_defaults(handler=_workspace)

    capture = subcommands.add_parser("capture", help="private capture operations")
    capture_commands = capture.add_subparsers(dest="capture_command", required=True)
    inspect = capture_commands.add_parser("inspect", help="inspect captures without exposing raw bytes")
    inspect.add_argument("--project-root", type=Path)
    inspect.add_argument("--json", action="store_true", dest="as_json")
    inspect.set_defaults(handler=_capture_inspect)

    export = subcommands.add_parser("export", help="generate the tracked project result")
    export.add_argument("--project-root", type=Path)
    export.add_argument("--json", action="store_true", dest="as_json")
    export.set_defaults(handler=_export)
    args = parser.parse_args()
    return vars(args)["handler"](args)


def _root(args: argparse.Namespace) -> Path:
    return args.project_root.resolve() if args.project_root else find_project_root()


def _load_catalog_rows(root: Path, path: Path) -> list[dict[str, Any]]:
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("catalog rows input must be one JSON array of objects")
    return value


def _catalog_import(args: argparse.Namespace) -> int:
    try:
        root = _root(args)
        repository = ProjectRepository(root)
        rows = _load_catalog_rows(root, args.catalog_rows)
        repository.rebuild_cache(rows, repository.paths.captures)
        return _emit_finite(
            {"schema_version": 1, "operation": "catalog_import", "catalog_rows": len(rows)},
            f"Imported {len(rows)} catalog rows and rebuilt the project cache.",
            args.as_json,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _finite_error(exc, args.as_json)


def _cache_rebuild(args: argparse.Namespace) -> int:
    try:
        root = _root(args)
        repository = ProjectRepository(root)
        rows = _load_catalog_rows(root, args.catalog_rows)
        repository.rebuild_cache(rows, repository.paths.captures)
        return _emit_finite(
            {"schema_version": 1, "operation": "cache_rebuild", "catalog_rows": len(rows)},
            f"Rebuilt the project cache from {len(rows)} catalog rows and private captures.",
            args.as_json,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _finite_error(exc, args.as_json)


def _plan(args: argparse.Namespace) -> int:
    try:
        root = _root(args)
        repository = ProjectRepository(root)
        output = (
            args.output.resolve() if args.output and args.output.is_absolute()
            else (root / args.output).resolve() if args.output
            else repository.paths.plans / "admitted-plan.json"
        )
        available = shutil.disk_usage(repository.paths.scratch).free // (1024 * 1024)
        plan, digest = compile_plan(
            repository,
            available_disk_mib=available,
            created_utc=_utc_now(),
            output=output,
        )
        summary = {
            "schema_version": 1,
            "operation": "plan",
            "admitted": True,
            "plan_sha256": digest,
            "endpoint_count": len(plan["endpoint_sessions"]),
            "acquisition_count": len(plan["acquisitions"]),
            "objective_count": len(plan["objectives"]),
            "output": output.relative_to(root).as_posix(),
        }
        return _emit_finite(summary, (
            f"Admitted {summary['objective_count']} objectives as "
            f"{summary['acquisition_count']} acquisitions across "
            f"{summary['endpoint_count']} endpoints. Plan: {summary['output']}"
        ), args.as_json)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _finite_error(exc, args.as_json)


def _capture_inspect(args: argparse.Namespace) -> int:
    try:
        root = _root(args)
        repository = ProjectRepository(root)
        result = inspect_capture_tree(repository.paths.captures, root)
        result = {"schema_version": 1, "operation": "capture_inspect", **result}
        return _emit_finite(
            result,
            f"Inspected {result['runs']} runs, {result['segments']} segments, and "
            f"{result['exchanges']} exchanges.",
            args.as_json,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _finite_error(exc, args.as_json)


def _export(args: argparse.Namespace) -> int:
    try:
        root = _root(args)
        repository = ProjectRepository(root)
        result = export_project(
            repository,
            toolkit_version=__version__,
            toolkit_commit=_toolkit_commit(root),
            generated_at_utc=_utc_now(),
        )
        summary = {
            "schema_version": 1,
            "operation": "export",
            "generation_id": result["generation_id"],
            "row_count": result["row_counts"]["signals"],
        }
        return _emit_finite(
            summary,
            f"Generated result {summary['generation_id']} with {summary['row_count']} signals.",
            args.as_json,
        )
    except (FileNotFoundError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return _finite_error(exc, args.as_json)


def _emit_finite(document: dict[str, Any], human: str, as_json: bool) -> int:
    print(json.dumps(document, sort_keys=True) if as_json else human)
    return 0


def _finite_error(exc: Exception, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"schema_version": 1, "error": str(exc)}, sort_keys=True))
    else:
        print(f"ERROR: {exc}", file=sys.stderr)
    return 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _toolkit_commit(project_root: Path) -> str:
    toolkit = ProjectRepository(project_root).paths.toolkit
    completed = subprocess.run(
        ["git", "-C", str(toolkit), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _workspace(args: argparse.Namespace) -> int:
    try:
        root = _root(args)
        return OperatorWorkspace(ProjectRepository(root)).run()
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


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
        root = _root(args)
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
