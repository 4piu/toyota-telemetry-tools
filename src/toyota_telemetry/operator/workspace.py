"""Create a project workspace: one folder holding every working file.

A workspace is private by default and lives outside any repository. Results
leave it only through an export of the result.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any

from ..i18n import _
from ..identity import stable_slug
from ..project import PROJECT_MARKER
from ..repository import ProjectPaths, ProjectRepository, _encoded_json
from ..schemas import POLICY_SCHEMA_VERSION, PROJECT_SCHEMA_VERSION


# Conservative defaults; a project tightens or relaxes them with evidence.
DEFAULT_SAFETY = {
    "did_max": "0xEFFF",
    "did_min": "0x0100",
    "initial_response_timeout_ms": 1000,
    "maximum_estimated_bus_utilization": 0.01,
    "maximum_nominal_session_utilization": 0.1,
    "maximum_request_rate_hz": 1,
    "request_start_floor_ms": 1000,
    "response_pending_budget_ms": 1000,
    "transport_retries": 0,
}
DEFAULT_STORAGE = {
    "capture_tree_max_mib": 2048,
    "disk_reserve_mib": 256,
    "segment_max_mib": 16,
    "segment_seconds": 60,
}


def slug_from_name(name: str) -> str:
    """A stable id suggested from a folder name."""

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "vehicle"


def create_workspace(root: Path, project_id: str | None = None, profile_id: str | None = None) -> ProjectRepository:
    """Create an empty workspace in a new or empty folder and open it."""

    root = Path(root).resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError(_("{name} is not an empty folder").format(name=root.name))
    project_id = stable_slug(project_id or slug_from_name(root.name), "project_id")
    profile_id = stable_slug(profile_id or project_id, "profile_id")
    paths = ProjectPaths.for_root(root)
    header = {"profile_id": profile_id, "schema_version": 1}
    documents: dict[Path, dict[str, Any]] = {
        paths.transport: {
            **header,
            "document_type": "telemetry.transport",
            "network": {"addressing": "normal", "bitrate": 500000, "type": "classical-can"},
            "safety": dict(DEFAULT_SAFETY),
            "session_recipes": [],
            "endpoints": [],
            "dynamic_dids": [],
        },
        paths.collection_policy: {
            **header,
            "schema_version": POLICY_SCHEMA_VERSION,
            "document_type": "telemetry.collection-policy",
            "storage": dict(DEFAULT_STORAGE),
            "acquisitions": [],
            "targets": [],
        },
        paths.signal_review: {**header, "document_type": "telemetry.signal-review", "signals": []},
        paths.catalog_manifest: {
            **header,
            "document_type": "telemetry.catalog-manifest",
            "catalog_row_count": 0,
            "catalog_rows_sha256": hashlib.sha256(b"[]\n").hexdigest(),
            "sources": [],
        },
    }
    for path, document in documents.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_encoded_json(document))
    paths.captures.mkdir()
    paths.state.mkdir()
    (root / PROJECT_MARKER).write_text(
        f"schema_version = {PROJECT_SCHEMA_VERSION}\nproject_id = \"{project_id}\"\n",
        encoding="utf-8", newline="\n",
    )
    return ProjectRepository(root)
