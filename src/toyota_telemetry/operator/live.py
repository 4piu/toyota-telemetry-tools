"""Live-operation ownership and exact-scope confirmation.

Only one live job may own a project at a time, whichever interface started
it. The lock is an operating-system file lock, so it disappears with a
crashed process instead of leaving a stale marker.

A scripted (non-interactive) live run must match the scope most recently
printed by ``--dry-run`` for the same command. The preview hash is recorded
automatically under ``scratch/`` so nobody types it.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from ..repository import ProjectRepository, _atomic_write, _encoded_json


LOCK_FILE = "live.lock"
PREVIEW_FILE = "live-previews.json"


class LiveBusy(RuntimeError):
    """Another live operation owns this project."""


class ConfirmationRefused(RuntimeError):
    """A live operation lacked the required confirmation."""


def scope_hash(scope: Mapping[str, Any]) -> str:
    encoded = json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class LiveLock:
    def __init__(self, repository: ProjectRepository) -> None:
        self.path = repository.paths.scratch / LOCK_FILE
        self._handle = None

    def __enter__(self) -> "LiveLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            _lock(handle)
        except OSError as exc:
            handle.close()
            raise LiveBusy("another live operation is running for this project") from exc
        self._handle = handle
        return self

    def __exit__(self, *_: Any) -> None:
        if self._handle is not None:
            try:
                _unlock(self._handle)
            finally:
                self._handle.close()
                self._handle = None


def live_busy(repository: ProjectRepository) -> bool:
    try:
        with LiveLock(repository):
            return False
    except LiveBusy:
        return True


def record_preview(repository: ProjectRepository, command: str, digest: str) -> None:
    path = repository.paths.scratch / PREVIEW_FILE
    previews = _read_previews(path)
    previews[command] = digest
    _atomic_write(path, _encoded_json(previews))


def require_previewed(repository: ProjectRepository, command: str, digest: str) -> None:
    recorded = _read_previews(repository.paths.scratch / PREVIEW_FILE).get(command)
    if recorded is None:
        raise ConfirmationRefused(
            f"no previewed {command} scope: run `telemetry {command} ... --dry-run`, review it, "
            "then repeat the command with --yes"
        )
    if recorded != digest:
        raise ConfirmationRefused(
            f"the {command} scope changed since the last --dry-run (selection, authorities, "
            "connector, or verification facts); preview it again before using --yes"
        )


def _read_previews(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}


if os.name == "nt":
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
