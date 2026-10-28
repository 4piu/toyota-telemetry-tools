"""Machine-local connector settings: fields, validation, and offline self-test.

Nothing here opens a device. ``check_connector`` validates the settings
document, the worker executable, kind-specific paths, and runs the worker's
``--self-test``, which exercises framing and the telemetry guard only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess
from typing import Any, Mapping

from ..repository import ProjectRepository
from ..schemas import validate_local_settings


CONNECTOR_FIELDS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "pcan": (("channel", "PCAN channel (PCAN_USBBUS1-8)", True),),
    "j2534": (("dll", "J2534 driver DLL path", True),),
    "gsusb": (
        ("vendor_id", "USB vendor ID (4 hex digits)", True),
        ("product_id", "USB product ID (4 hex digits)", True),
        ("serial", "USB serial number (optional)", False),
    ),
    "slcan": (("port", "Serial port, e.g. COM5", True),),
}
CONNECTOR_LABELS = {
    "pcan": "PCAN-USB",
    "j2534": "J2534 pass-through",
    "gsusb": "gs_usb / candleLight",
    "slcan": "slcan (LAWICEL serial)",
}


@dataclass
class ConnectorCheck:
    ok: bool
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "problems": list(self.problems), "notes": list(self.notes)}


def default_executable(repository: ProjectRepository, kind: str) -> str:
    scratch = repository.paths.scratch.relative_to(repository.root).as_posix()
    return f"{scratch}/bin/telemetry-{kind}.exe"


def connector_document(
    repository: ProjectRepository,
    kind: str,
    settings: Mapping[str, str],
    executable: str | None = None,
) -> dict[str, Any]:
    if kind not in CONNECTOR_FIELDS:
        raise ValueError(f"unknown connector {kind!r}; choose {', '.join(CONNECTOR_FIELDS)}")
    allowed = {name for name, _, _ in CONNECTOR_FIELDS[kind]}
    clean = {name: str(value).strip() for name, value in settings.items() if str(value).strip()}
    if unknown := sorted(set(clean) - allowed):
        raise ValueError(f"{kind} does not use: {', '.join(unknown)}")
    if kind == "gsusb":
        for name in ("vendor_id", "product_id"):
            if name in clean:
                clean[name] = clean[name].lower().removeprefix("0x")
    document = {
        "document_type": "telemetry.local-settings",
        "schema_version": 1,
        "project_id": repository.project_id,
        "backend": {
            "kind": kind,
            "executable": executable or default_executable(repository, kind),
            "settings": clean,
        },
    }
    validate_local_settings(document)
    return document


def resolve_executable(repository: ProjectRepository, document: Mapping[str, Any]) -> Path:
    path = Path(document["backend"]["executable"])
    return path if path.is_absolute() else (repository.root / path).resolve()


def check_connector(
    repository: ProjectRepository,
    document: Mapping[str, Any] | None,
    *,
    run_self_test: bool = True,
) -> ConnectorCheck:
    if document is None:
        return ConnectorCheck(False, ["no connector is configured; run `telemetry connector set`"])
    check = ConnectorCheck(True)
    try:
        validate_local_settings(document)
    except ValueError as exc:
        return ConnectorCheck(False, [str(exc)])
    backend = document["backend"]
    executable = resolve_executable(repository, document)
    if not executable.is_file():
        check.problems.append(
            f"worker {executable.name} is missing; run the project setup script to build it"
        )
    if backend["kind"] == "j2534":
        dll = Path(backend["settings"]["dll"])
        if not dll.is_file():
            check.problems.append(f"J2534 driver DLL was not found: {dll.name}")
    if run_self_test and executable.is_file():
        try:
            completed = subprocess.run(
                [str(executable), "--self-test"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            check.problems.append(f"worker self-test could not run: {exc}")
        else:
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip().splitlines()
                check.problems.append(
                    "worker self-test failed" + (f": {detail[-1]}" if detail else "")
                )
            else:
                check.notes.append("worker self-test passed; no device was opened")
    check.ok = not check.problems
    return check
