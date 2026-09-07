"""Project-root and configuration helpers.

No helper derives a path from the checkout directory's name or parent layout.
All relative paths are anchored at the directory containing
``telemetry-project.toml``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT_MARKER = "telemetry-project.toml"


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
