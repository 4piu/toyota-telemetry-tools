"""Project-root and configuration helpers.

No helper derives a path from the workspace directory's name or parent
layout. All relative paths are anchored at the directory containing
``telemetry-project.toml``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


PROJECT_MARKER = "telemetry-project.toml"
# Workers ship inside the package; ``backends/build.ps1`` builds them here.
WORKERS = Path(__file__).resolve().parent / "workers"


def worker_executable(root: Path, backend: Mapping[str, Any]) -> Path:
    """The worker for a connector: bundled unless the settings name another."""

    if backend["executable"] is None:
        return WORKERS / f"telemetry-{backend['kind']}.exe"
    path = Path(backend["executable"])
    return path if path.is_absolute() else (root / path).resolve()


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / PROJECT_MARKER).is_file():
            return candidate
    raise FileNotFoundError(
        f"could not find {PROJECT_MARKER} at or above {current}; "
        "run from a telemetry project or pass --project-root"
    )


def atomic_json(path: Path, document: dict[str, Any], replace: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise FileExistsError(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
