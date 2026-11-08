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

from ..i18n import _, N_
from ..project import worker_executable
from ..repository import ProjectRepository
from ..schemas import validate_local_settings


CONNECTOR_FIELDS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "pcan": (("channel", N_("PCAN channel (PCAN_USBBUS1-8)"), True),),
    "j2534": (("dll", N_("J2534 driver DLL path"), True),),
    "gsusb": (
        ("vendor_id", N_("USB vendor ID (4 hex digits)"), True),
        ("product_id", N_("USB product ID (4 hex digits)"), True),
        ("serial", N_("USB serial number (optional)"), False),
    ),
    "slcan": (("port", N_("Serial port, e.g. COM5"), True),),
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




def connector_document(
    repository: ProjectRepository,
    kind: str,
    settings: Mapping[str, str],
    executable: str | None = None,
) -> dict[str, Any]:
    if kind not in CONNECTOR_FIELDS:
        raise ValueError(_("unknown connector {kind!r}; choose {kinds}").format(
            kind=kind, kinds=", ".join(CONNECTOR_FIELDS),
        ))
    allowed = {name for name, _label, _required in CONNECTOR_FIELDS[kind]}
    clean = {name: str(value).strip() for name, value in settings.items() if str(value).strip()}
    if unknown := sorted(set(clean) - allowed):
        raise ValueError(_("{kind} does not use: {fields}").format(kind=kind, fields=", ".join(unknown)))
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
            "executable": executable or None,
            "settings": clean,
        },
    }
    validate_local_settings(document)
    return document


def resolve_executable(repository: ProjectRepository, document: Mapping[str, Any]) -> Path:
    return worker_executable(repository.root, document["backend"])


def check_connector(
    repository: ProjectRepository,
    document: Mapping[str, Any] | None,
    *,
    run_self_test: bool = True,
) -> ConnectorCheck:
    if document is None:
        return ConnectorCheck(False, [_("no connector is configured")])
    check = ConnectorCheck(True)
    try:
        validate_local_settings(document)
    except ValueError as exc:
        return ConnectorCheck(False, [str(exc)])
    backend = document["backend"]
    executable = resolve_executable(repository, document)
    if not executable.is_file():
        check.problems.append(
            _("worker {name} is missing from this installation").format(name=executable.name)
        )
    if backend["kind"] == "j2534":
        dll = Path(backend["settings"]["dll"])
        if not dll.is_file():
            check.problems.append(_("J2534 driver DLL was not found: {name}").format(name=dll.name))
    if run_self_test and executable.is_file():
        try:
            completed = subprocess.run(
                [str(executable), "--self-test"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            check.problems.append(_("worker self-test could not run: {error}").format(error=exc))
        else:
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip().splitlines()
                check.problems.append(
                    _("worker self-test failed: {detail}").format(detail=detail[-1]) if detail
                    else _("worker self-test failed")
                )
            else:
                check.notes.append(_("worker self-test passed; no device was opened"))
    check.ok = not check.problems
    return check
