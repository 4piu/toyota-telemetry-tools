"""Bounded live endpoint-presence discovery and DID preflight.

Both operations are explicitly live: `telemetry discover endpoints` opens the
configured local adapter against operator-supplied candidate CAN ID pairs and
replays an already-reviewed session recipe from tracked transport authority;
`telemetry preflight dids` does the same against a confirmed endpoint or a
responding discovery candidate, plus one additional ReadDataByIdentifier per
requested DID. Neither authors collection objectives or edits tracked
transport authority; results land as provisional SQLite facts (see
`ProjectRepository.record_discovery_candidate` / `record_did_preflight`)
until an explicit `promote_confirmed_endpoint` review action writes a
confirmed endpoint into `config/transport.json`.

There is no automatic CAN ID sweep: every candidate and every DID is named
explicitly by the operator, keeping each run a small, bounded, reviewable
set of transactions rather than an open-ended bus scan.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable, Mapping, Protocol

from .repository import ProjectRepository
from .worker_protocol import build_probe_config, parse_probe_result, write_probe_config


class ProbeWorker(Protocol):
    def probe(self) -> Mapping[str, Any]: ...


ProbeWorkerFactory = Callable[[Path], ProbeWorker]


class JsonlProbeClient:
    """Blocking client for one private C# adapter probe subprocess. Unlike
    the long-lived collect worker, a probe takes no stdin commands: it opens,
    attempts its configured steps, prints exactly one result line, and
    exits."""

    def __init__(self, executable: Path, config_path: Path) -> None:
        self.executable = executable
        self.config_path = config_path

    def probe(self) -> Mapping[str, Any]:
        completed = subprocess.run(
            [str(self.executable), "--probe", "--config", str(self.config_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit code {completed.returncode}"
            raise RuntimeError(f"probe process failed: {detail}")
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise RuntimeError("probe process must emit exactly one result line")
        return parse_probe_result(lines[0])


def _local_backend(repository: ProjectRepository) -> Mapping[str, Any]:
    if repository.local_settings is None:
        raise ValueError("local settings are required for a live probe")
    return repository.local_settings["backend"]


def default_probe_worker_factory(
    repository: ProjectRepository, root: Path
) -> ProbeWorkerFactory:
    backend = _local_backend(repository)
    executable = Path(backend["executable"])
    if not executable.is_absolute():
        executable = (root / executable).resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)

    def factory(config_path: Path) -> ProbeWorker:
        return JsonlProbeClient(executable, config_path)

    return factory


def discover_endpoints(
    repository: ProjectRepository,
    root: Path,
    candidates: list[tuple[str, str, str]],
    *,
    utc_now: Callable[[], str],
    probe_worker_factory: ProbeWorkerFactory | None = None,
) -> list[dict[str, Any]]:
    """Run one bounded probe per (tester_can_id, response_can_id,
    session_recipe_id) candidate and record a provisional fact for each."""

    if not candidates:
        raise ValueError("telemetry discover endpoints requires at least one --candidate")
    backend = _local_backend(repository)
    factory = probe_worker_factory or default_probe_worker_factory(repository, root)
    results = []
    for tester_can_id, response_can_id, session_recipe_id in candidates:
        candidate_id = f"{tester_can_id}:{response_can_id}"
        config = build_probe_config(
            repository.transport, candidate_id, tester_can_id, response_can_id,
            session_recipe_id, backend,
        )
        result = _run_probe(repository, factory, config)
        fact = {
            "candidate_id": candidate_id,
            "tester_can_id": tester_can_id,
            "response_can_id": response_can_id,
            "session_recipe_id": session_recipe_id,
            "responded": result["responded"],
            "observed_utc": utc_now(),
            "result": result,
        }
        repository.record_discovery_candidate(fact)
        results.append(fact)
    return results


def preflight_dids(
    repository: ProjectRepository,
    root: Path,
    endpoint_id: str,
    data_ids: list[str],
    *,
    utc_now: Callable[[], str],
    probe_worker_factory: ProbeWorkerFactory | None = None,
) -> list[dict[str, Any]]:
    """Probe one or more DIDs against a confirmed endpoint or a responding
    discovery candidate and record a provisional fact for each. Never
    authors a collection objective from the outcome."""

    if not data_ids:
        raise ValueError("telemetry preflight dids requires at least one --did")
    tester_can_id, response_can_id, session_recipe_id = _resolve_preflight_target(
        repository, endpoint_id
    )
    backend = _local_backend(repository)
    factory = probe_worker_factory or default_probe_worker_factory(repository, root)
    results = []
    for data_id in data_ids:
        candidate_id = f"{endpoint_id}/{data_id}"
        config = build_probe_config(
            repository.transport, candidate_id, tester_can_id, response_can_id,
            session_recipe_id, backend, read_data_id=data_id,
        )
        result = _run_probe(repository, factory, config)
        read = result["read"]
        outcome = read["outcome"] if read is not None else "session_unavailable"
        fact = {
            "endpoint_id": endpoint_id,
            "data_id": data_id,
            "outcome": outcome,
            "observed_utc": utc_now(),
            "result": result,
        }
        repository.record_did_preflight(fact)
        results.append(fact)
    return results


def _run_probe(
    repository: ProjectRepository, factory: ProbeWorkerFactory, config: Mapping[str, Any]
) -> Mapping[str, Any]:
    repository.paths.plans.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".probe-", dir=repository.paths.plans) as directory:
        config_path = Path(directory) / "config.json"
        write_probe_config(config_path, config)
        worker = factory(config_path)
        return worker.probe()


def _resolve_preflight_target(
    repository: ProjectRepository, endpoint_id: str
) -> tuple[str, str, str]:
    for endpoint in repository.transport["endpoints"]:
        if endpoint["id"] == endpoint_id:
            return (
                endpoint["tester_can_id"], endpoint["response_can_id"],
                endpoint["session_recipe_id"],
            )
    for candidate in repository.discovery_candidates():
        if candidate["candidate_id"] == endpoint_id:
            if not candidate["responded"]:
                raise ValueError(f"discovery candidate did not respond: {endpoint_id}")
            return (
                candidate["tester_can_id"], candidate["response_can_id"],
                candidate["session_recipe_id"],
            )
    raise ValueError(
        f"preflight target is neither a confirmed endpoint nor a discovery candidate: {endpoint_id}"
    )


def _slug_can_id(value: str) -> str:
    text = value.lower()
    return text[2:] if text.startswith("0x") else text


def promote_confirmed_endpoint(
    transport: Mapping[str, Any],
    candidate: Mapping[str, Any],
    ecu_keys: list[int],
    provenance: list[str],
) -> dict[str, Any]:
    """Build an updated transport document promoting one responding
    discovery candidate into a tracked confirmed endpoint.

    A pure transform: the caller writes the result through
    `ProjectRepository.write_authored_document("transport", ...)`, which
    validates and atomically replaces the tracked file. This is the explicit
    review action the specification requires; a live discovery/preflight
    command never writes tracked transport authority itself.
    """

    if not candidate["responded"]:
        raise ValueError("only a responding discovery candidate may be promoted")
    if not ecu_keys:
        raise ValueError("promoting an endpoint requires at least one ecu_key")
    if not provenance:
        raise ValueError("promoting an endpoint requires at least one provenance reference")
    endpoint_id = (
        f"{_slug_can_id(candidate['tester_can_id'])}-{_slug_can_id(candidate['response_can_id'])}"
    )
    existing_ids = {row["id"] for row in transport["endpoints"]}
    if endpoint_id in existing_ids:
        raise ValueError(f"endpoint id already confirmed: {endpoint_id}")
    existing_ecu = {ecu for row in transport["endpoints"] for ecu in row["ecu_keys"]}
    conflicting = sorted(set(ecu_keys) & existing_ecu)
    if conflicting:
        raise ValueError(f"ECU keys already belong to a confirmed endpoint: {conflicting}")
    recipe_ids = {row["id"] for row in transport["session_recipes"]}
    if candidate["session_recipe_id"] not in recipe_ids:
        raise ValueError(f"unknown session_recipe_id: {candidate['session_recipe_id']}")

    new_endpoint = {
        "id": endpoint_id,
        "tester_can_id": candidate["tester_can_id"],
        "response_can_id": candidate["response_can_id"],
        "session_recipe_id": candidate["session_recipe_id"],
        "ecu_keys": sorted(ecu_keys),
        "state": "confirmed",
        "provenance": list(provenance),
    }
    endpoints = sorted([*transport["endpoints"], new_endpoint], key=lambda row: row["id"])
    return {**transport, "endpoints": endpoints}
