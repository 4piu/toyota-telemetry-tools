"""Shared live collection service used by CLI and standalone operator apps."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
import threading
from typing import Callable
import uuid

from .capture_writer import NativeRunWriter
from .collector import CollectionRunner, JsonlWorkerClient, load_verified_plan
from .repository import ProjectRepository
from .worker_protocol import build_worker_config, write_worker_config


EventSink = Callable[[dict], None]


def run_collection(
    project_root: Path,
    *,
    plan_path: Path | None = None,
    endpoint_id: str | None = None,
    run_note: str | None = None,
    event_sink: EventSink,
    stop_event: threading.Event,
) -> str:
    """Run one admitted endpoint plan without depending on a CLI parser."""

    root = project_root.resolve()
    repository = ProjectRepository(root)
    if repository.local_settings is None:
        raise ValueError("local settings are required for live collection")
    resolved_plan = (
        plan_path.resolve()
        if plan_path is not None and plan_path.is_absolute()
        else (root / plan_path).resolve()
        if plan_path is not None
        else repository.paths.plans / "admitted-plan.json"
    )
    plan, plan_bytes = load_verified_plan(resolved_plan, repository)
    endpoint_ids = [row["endpoint_id"] for row in plan["endpoint_sessions"]]
    if endpoint_id is None:
        if len(endpoint_ids) != 1:
            raise ValueError("endpoint_id is required for a multi-endpoint admitted plan")
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
    event_sink({
        "event": "scope",
        "schema_version": 1,
        "run_id": run_id,
        "endpoint_id": endpoint_id,
        "backend_kind": backend["kind"],
        "capture_root": repository.paths.captures.relative_to(root).as_posix(),
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
    })
    worker_config = build_worker_config(plan, endpoint_id, backend)
    with tempfile.TemporaryDirectory(
        prefix=".worker-", dir=repository.paths.plans
    ) as directory:
        worker_config_path = Path(directory) / "config.json"
        write_worker_config(worker_config_path, worker_config)
        writer = NativeRunWriter(
            repository.paths.captures,
            plan,
            plan_bytes,
            run_id=run_id,
            created_utc=created,
            backend_kind=backend["kind"],
            endpoint_id=endpoint_id,
            run_note=run_note,
        )
        worker = JsonlWorkerClient(executable, worker_config_path)
        try:
            runner = CollectionRunner(
                plan,
                writer,
                worker,
                event_sink=event_sink,
                stop_event=stop_event,
            )
        except Exception:
            writer.crash()
            raise
        return runner.run()
