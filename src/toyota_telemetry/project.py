"""Project-root and configuration helpers.

No helper derives a path from the checkout directory's name or parent layout.
All relative paths are anchored at the directory containing
``telemetry-project.toml``.
"""

from __future__ import annotations

import json
from pathlib import Path
import tomllib
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


def project_root(explicit: Path | None, reference: Path | None = None) -> Path:
    root = explicit.resolve() if explicit else find_project_root(reference)
    if not (root / PROJECT_MARKER).is_file():
        raise FileNotFoundError(f"project marker not found: {root / PROJECT_MARKER}")
    return root


def resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def load_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    return document


def load_project_marker(root: Path) -> dict[str, Any]:
    with (root / PROJECT_MARKER).open("rb") as handle:
        marker = tomllib.load(handle)
    if marker.get("schema_version") != 1:
        raise ValueError("only telemetry-project schema 1 is supported")
    return marker


def atomic_json(path: Path, document: dict[str, Any], replace: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise FileExistsError(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)

