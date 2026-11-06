"""Canonical project repository and derived SQLite query cache."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import tomllib
from typing import Any, Iterable, Mapping

from .capture_store import capture_run_hashes, capture_set_hash, load_capture_tree
from .identity import AcquisitionRef, SignalRef, canonical_did
from .objectives import ReplayObservation, condition_refs, replay_targets
from .schemas import (
    validate_local_settings,
    validate_policy,
    validate_project_marker,
    validate_review,
    validate_transport,
)


CACHE_SCHEMA_VERSION = 4
VERIFICATION_FACTS_FILE = "verification-facts.json"
# Tables filled from the curated inputs, children before parents.
_CURATED_TABLES = (
    "catalog_variant",
    "signal_review",
    "policy_target",
    "policy_acquisition",
    "dynamic_did",
    "endpoint_ecu",
    "endpoint",
    "session_recipe",
)
_CACHE_TABLES = (
    "schema_meta",
    "source_file",
    "session_recipe",
    "endpoint",
    "endpoint_ecu",
    "dynamic_did",
    "policy_acquisition",
    "policy_target",
    "signal_review",
    "catalog_variant",
    "run",
    "segment",
    "acquisition_exchange",
    "signal_observation",
    "did_status",
    "target_progress",
    "export_generation",
)


# How long a connection waits for another one (the desktop application and a
# CLI command on the same workspace) to finish writing the cache. Refreshing a
# large cache takes longer than SQLite's 5 s default.
CACHE_BUSY_TIMEOUT_SECONDS = 30


def connect_cache(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=CACHE_BUSY_TIMEOUT_SECONDS)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _exact_keys(value: Mapping[str, Any], keys: set[str], where: str) -> None:
    if set(value) != keys:
        raise ValueError(f"{where} fields mismatch: expected {sorted(keys)}, got {sorted(value)}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exact_json(path: Path) -> tuple[dict[str, Any], bytes]:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{path}: UTF-8 BOM is not allowed")
    if not data.endswith(b"\n") or data.endswith(b"\n\n"):
        raise ValueError(f"{path}: document must end with exactly one newline")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: document must be an object")
    return value, data


def _encoded_json(value: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"document is not finite JSON: {exc}") from exc
    return (text + "\n").encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ProjectPaths:
    """The fixed workspace layout; every path is inside the workspace."""

    transport: Path
    collection_policy: Path
    signal_review: Path
    catalog_manifest: Path
    captures: Path
    state: Path
    local_settings: Path
    database: Path
    plans: Path

    @classmethod
    def for_root(cls, root: Path) -> "ProjectPaths":
        state = root / "state"
        return cls(
            transport=root / "config" / "transport.json",
            collection_policy=root / "config" / "collection-policy.json",
            signal_review=root / "review" / "signals.json",
            catalog_manifest=root / "config" / "catalog-manifest.json",
            captures=root / "captures",
            state=state,
            local_settings=state / "local-settings.json",
            database=state / "project.sqlite",
            plans=state / "plans",
        )


class ProjectRepository:
    """Validated workspace accessed through one repository boundary."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        marker_path = self.root / "telemetry-project.toml"
        try:
            with marker_path.open("rb") as handle:
                marker = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{marker_path}: invalid TOML: {exc}") from exc
        validate_project_marker(marker)
        self.marker = marker
        self.project_id = str(marker["project_id"])
        self.paths = ProjectPaths.for_root(self.root)

        self.transport, self._transport_bytes = _exact_json(self.paths.transport)
        self.policy, self._policy_bytes = _exact_json(self.paths.collection_policy)
        self.review, self._review_bytes = _exact_json(self.paths.signal_review)
        validate_transport(self.transport)
        validate_policy(self.policy)
        validate_review(self.review)
        profile_ids = {
            self.transport["profile_id"],
            self.policy["profile_id"],
            self.review["profile_id"],
        }
        if len(profile_ids) != 1:
            raise ValueError("transport, policy, and review profile_id values must match")
        self.profile_id = str(next(iter(profile_ids)))

        self.local_settings: dict[str, Any] | None = None
        self._local_settings_bytes: bytes | None = None
        if self.paths.local_settings.is_file():
            self.local_settings, self._local_settings_bytes = _exact_json(
                self.paths.local_settings
            )
            validate_local_settings(self.local_settings)
            if self.local_settings["project_id"] != self.project_id:
                raise ValueError("local settings project_id does not match project marker")

        if not self.paths.catalog_manifest.is_file():
            raise FileNotFoundError(self.paths.catalog_manifest)
        self._catalog_manifest_bytes = self.paths.catalog_manifest.read_bytes()
        self._validate_references()

    def _validate_references(self) -> None:
        endpoint_ids = {row["id"] for row in self.transport["endpoints"]}
        ecu_to_endpoint = {
            int(ecu): row["id"]
            for row in self.transport["endpoints"]
            for ecu in row["ecu_keys"]
        }
        did_min = int(self.transport["safety"]["did_min"], 16)
        did_max = int(self.transport["safety"]["did_max"], 16)

        for row in self.policy["acquisitions"]:
            ref = AcquisitionRef.parse(row["acquisition_id"])
            if ref.endpoint_id not in endpoint_ids:
                raise ValueError(f"unresolved policy acquisition endpoint: {ref}")
            if not did_min <= ref.data_id <= did_max:
                raise ValueError(f"policy acquisition is outside the DID safety range: {ref}")

        for row in self.policy["targets"]:
            self._validate_signal_ref(row["signal_ref"], ecu_to_endpoint, did_min, did_max)
            if row["condition"] is not None:
                for signal_ref in condition_refs(row["condition"]):
                    self._validate_signal_ref(signal_ref, ecu_to_endpoint, did_min, did_max)
        for row in self.review["signals"]:
            self._validate_signal_ref(row["signal_ref"], ecu_to_endpoint, did_min, did_max)

    def write_authored_document(self, kind: str, document: Mapping[str, Any]) -> None:
        """Validate and atomically replace one authored JSON document."""

        routes = {
            "transport": (self.paths.transport, validate_transport, "transport", "_transport_bytes"),
            "collection_policy": (
                self.paths.collection_policy,
                validate_policy,
                "policy",
                "_policy_bytes",
            ),
            "signal_review": (self.paths.signal_review, validate_review, "review", "_review_bytes"),
            "local_settings": (
                self.paths.local_settings,
                validate_local_settings,
                "local_settings",
                "_local_settings_bytes",
            ),
        }
        if kind not in routes:
            raise ValueError(f"unsupported authored document kind: {kind}")
        path, validator, attribute, bytes_attribute = routes[kind]
        candidate = json.loads(_encoded_json(document))
        validator(candidate)
        if kind == "local_settings":
            if candidate["project_id"] != self.project_id:
                raise ValueError("local settings project_id does not match project marker")
        elif candidate["profile_id"] != self.profile_id:
            raise ValueError(f"{kind} profile_id does not match the project profile")

        previous = getattr(self, attribute)
        setattr(self, attribute, candidate)
        try:
            if kind != "local_settings":
                self._validate_references()
        except Exception:
            setattr(self, attribute, previous)
            raise

        encoded = _encoded_json(candidate)
        try:
            _atomic_write(path, encoded)
        except Exception:
            setattr(self, attribute, previous)
            raise
        setattr(self, bytes_attribute, encoded)

    @staticmethod
    def _validate_signal_ref(
        value: str, ecu_to_endpoint: Mapping[int, str], did_min: int, did_max: int
    ) -> None:
        ref = SignalRef.parse(value)
        if ref.ecu_key not in ecu_to_endpoint:
            raise ValueError(f"unresolved signal_ref ECU: {ref}")
        if not did_min <= ref.data_id <= did_max:
            raise ValueError(f"signal_ref is outside the DID safety range: {ref}")

    def source_hashes(
        self,
        catalog_rows: Iterable[Mapping[str, Any]] = (),
        captures_root: Path | None = None,
    ) -> dict[str, str]:
        rows = _normalized_catalog_rows(catalog_rows)
        hashes = {
            "transport": _sha256(self._transport_bytes),
            "collection_policy": _sha256(self._policy_bytes),
            "signal_review": _sha256(self._review_bytes),
            "catalog_manifest": _sha256(self._catalog_manifest_bytes),
            "catalog_rows": _sha256((_canonical_json(rows) + "\n").encode("utf-8")),
            "capture_set": capture_set_hash(captures_root),
        }
        return hashes

    def rebuild_cache(
        self,
        catalog_rows: Iterable[Mapping[str, Any]] = (),
        captures_root: Path | None = None,
    ) -> None:
        rows = _normalized_catalog_rows(catalog_rows)
        self._validate_catalog_rows(rows)
        database = self.paths.database
        database.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=database.name + ".", suffix=".tmp", dir=database.parent)
        os.close(handle)
        temporary = Path(name)
        temporary.unlink()
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            _create_cache_schema(connection)
            self._populate_cache(connection, rows, captures_root)
            connection.execute(f"PRAGMA user_version = {CACHE_SCHEMA_VERSION}")
            connection.commit()
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("project cache failed foreign-key validation")
        except Exception:
            connection.close()
            temporary.unlink(missing_ok=True)
            raise
        connection.close()
        try:
            os.replace(temporary, database)
        finally:
            temporary.unlink(missing_ok=True)
        if self.policy["targets"]:
            self.replay_progress()

    def refresh_cache(
        self,
        catalog_rows: Iterable[Mapping[str, Any]] = (),
        captures_root: Path | None = None,
    ) -> bool:
        """Bring the cache up to date; True when anything had to change.

        Only what changed is redone: changed curated inputs replace their own
        small tables, and changed captures re-read only the runs that were
        added, removed, or rewritten. A missing cache, another schema version,
        or an unreadable database is rebuilt from scratch.
        """

        rows = _normalized_catalog_rows(catalog_rows)
        self._validate_catalog_rows(rows)
        if self.paths.database.is_file():
            try:
                with closing(connect_cache(self.paths.database)) as connection:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    current = dict(connection.execute("SELECT kind, sha256 FROM source_file"))
                    wanted = self.source_hashes(rows, captures_root)
                    if version == CACHE_SCHEMA_VERSION and current == wanted:
                        return False
                    if version == CACHE_SCHEMA_VERSION and set(current) == set(wanted):
                        self._update_cache(connection, rows, captures_root, current, wanted)
                        if self.policy["targets"]:
                            self.replay_progress()
                        return True
            except sqlite3.Error:
                pass
        self.rebuild_cache(rows, captures_root)
        return True

    def _update_cache(
        self,
        connection: sqlite3.Connection,
        rows: list[dict[str, Any]],
        captures_root: Path | None,
        current: Mapping[str, str],
        wanted: Mapping[str, str],
    ) -> None:
        """Replace what changed in one transaction; the result equals a rebuild."""

        changed = {kind for kind in wanted if current.get(kind) != wanted[kind]}
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            # Derived tables point at curated and capture rows; they are
            # recomputed below (target progress by the caller's replay).
            connection.execute("DELETE FROM target_progress")
            connection.execute("DELETE FROM did_status")
            if changed - {"capture_set"}:
                for table in _CURATED_TABLES:
                    connection.execute(f"DELETE FROM {table}")
                self._insert_curated(connection, rows)
            if "capture_set" in changed:
                self._sync_capture_runs(connection, captures_root)
            self._rebuild_did_status(connection)
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("project cache failed foreign-key validation")
            connection.executemany(
                "UPDATE source_file SET sha256 = ? WHERE kind = ?",
                ((wanted[kind], kind) for kind in sorted(changed)),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _sync_capture_runs(self, connection: sqlite3.Connection, captures_root: Path | None) -> None:
        """Drop runs that are gone or changed on disk, then read the new ones."""

        present = capture_run_hashes(captures_root)
        stored = dict(connection.execute("SELECT run_id, files_sha256 FROM run"))
        for run_id, digest in stored.items():
            if present.get(run_id) != digest:
                for table in ("signal_observation", "acquisition_exchange", "segment", "run"):
                    connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        unchanged = {run_id for run_id, digest in stored.items() if present.get(run_id) == digest}
        names = set(present) - unchanged
        if captures_root is not None and names:
            self._insert_capture_runs(connection, captures_root, names, present)

    def ingest_captures(self, captures_root: Path) -> None:
        """Incrementally ingest immutable runs into an existing cache."""

        with closing(connect_cache(self.paths.database)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA user_version").fetchone()[0] != CACHE_SCHEMA_VERSION:
                raise ValueError("project cache schema version mismatch")
            self._insert_capture_runs(connection, captures_root, None, capture_run_hashes(captures_root))
            self._rebuild_did_status(connection)
            connection.execute(
                "INSERT OR REPLACE INTO source_file(kind, sha256) VALUES ('capture_set', ?)",
                (capture_set_hash(captures_root),),
            )
            connection.commit()
        if self.policy["targets"]:
            self.replay_progress()

    def _require_cache(self) -> None:
        if not self.paths.database.is_file():
            raise ValueError("project cache is missing; run telemetry cache rebuild first")
        with closing(connect_cache(self.paths.database)) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != CACHE_SCHEMA_VERSION:
                raise ValueError("project cache schema version mismatch; run telemetry cache rebuild")

    def ensure_cache_ready(self) -> None:
        """Require the exact current cache schema before a cache-backed operation."""

        self._require_cache()

    @property
    def verification_facts_path(self) -> Path:
        return self.paths.state / VERIFICATION_FACTS_FILE

    def _verification_facts(self) -> dict[str, Any]:
        path = self.verification_facts_path
        if not path.is_file():
            return {
                "document_type": "telemetry.verification-facts",
                "schema_version": 1,
                "project_id": self.project_id,
                "discovery": {},
                "preflight": {},
            }
        document, _ = _exact_json(path)
        _exact_keys(
            document,
            {"document_type", "schema_version", "project_id", "discovery", "preflight"},
            "verification facts",
        )
        if (
            document["document_type"] != "telemetry.verification-facts"
            or document["schema_version"] != 1
            or document["project_id"] != self.project_id
        ):
            raise ValueError(f"{path}: verification facts do not belong to this project")
        return document

    def _write_verification_facts(self, document: Mapping[str, Any]) -> None:
        _atomic_write(self.verification_facts_path, _encoded_json(document))

    def record_discovery_candidate(self, fact: Mapping[str, Any]) -> None:
        """Store one provisional endpoint-probe result.

        Verification facts are provisional machine-local observations kept in
        ignored state beside, not inside, the rebuildable cache, so rebuilding
        the cache or confirming an endpoint never discards them.
        """

        _exact_keys(
            fact,
            {
                "candidate_id", "tester_can_id", "response_can_id", "session_recipe_id",
                "responded", "observed_utc", "result",
            },
            "discovery candidate fact",
        )
        document = self._verification_facts()
        document["discovery"][fact["candidate_id"]] = json.loads(_canonical_json(fact))
        self._write_verification_facts(document)

    def discovery_candidates(self) -> list[dict[str, Any]]:
        facts = self._verification_facts()["discovery"]
        return [facts[key] for key in sorted(facts)]

    def record_did_preflight(self, fact: Mapping[str, Any]) -> None:
        """Store one provisional DID-probe result beside the cache."""

        _exact_keys(
            fact,
            {"endpoint_id", "data_id", "outcome", "observed_utc", "result"},
            "DID preflight fact",
        )
        document = self._verification_facts()
        key = f"{fact['endpoint_id']}/{canonical_did(fact['data_id'])}"
        document["preflight"][key] = json.loads(_canonical_json(fact))
        self._write_verification_facts(document)

    def did_preflight_facts(self, endpoint_id: str | None = None) -> list[dict[str, Any]]:
        facts = self._verification_facts()["preflight"]
        rows = [facts[key] for key in sorted(facts)]
        if endpoint_id is not None:
            rows = [row for row in rows if row["endpoint_id"] == endpoint_id]
        return rows

    def _validate_catalog_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        endpoint_by_ecu = {
            int(ecu): endpoint["id"]
            for endpoint in self.transport["endpoints"]
            for ecu in endpoint["ecu_keys"]
        }
        did_min = int(self.transport["safety"]["did_min"], 16)
        did_max = int(self.transport["safety"]["did_max"], 16)
        by_signal: dict[str, list[dict[str, Any]]] = {}
        by_variant: dict[str, dict[str, Any]] = {}
        for row in rows:
            self._validate_signal_ref(row["signal_ref"], endpoint_by_ecu, did_min, did_max)
            by_signal.setdefault(row["signal_ref"], []).append(row)
            by_variant[row["catalog_variant_id"]] = row

        required_refs = {row["signal_ref"] for row in self.policy["targets"]}
        required_refs.update(row["signal_ref"] for row in self.review["signals"])
        for target in self.policy["targets"]:
            if target["condition"] is not None:
                required_refs.update(condition_refs(target["condition"]))
        missing = sorted(required_refs - by_signal.keys())
        if missing:
            raise ValueError(f"catalog does not resolve signal_ref: {', '.join(missing)}")

        for review in self.review["signals"]:
            signal_ref = review["signal_ref"]
            selected = review["catalog_variant_id"]
            if selected is not None:
                variant = by_variant.get(selected)
                if variant is None or variant["signal_ref"] != signal_ref:
                    raise ValueError(
                        f"review catalog_variant_id does not resolve for {signal_ref}: {selected}"
                    )
                continue
            variants = by_signal[signal_ref]
            conflicts = {
                field
                for field in ("name", "formula", "unit", "signedness", "enumeration")
                if len({_canonical_json(row.get(field)) for row in variants}) > 1
            }
            resolved = set()
            if review["display_name_override"] is not None:
                resolved.add("name")
            if review["formula_override"] is not None:
                resolved.update({"formula", "signedness", "enumeration"})
            if review["unit_override"] is not None:
                resolved.add("unit")
            unresolved = sorted(conflicts - resolved)
            if unresolved:
                raise ValueError(
                    f"catalog variants conflict for {signal_ref}: "
                    + ", ".join(unresolved)
                )

    def cache_snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        with closing(connect_cache(self.paths.database)) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != CACHE_SCHEMA_VERSION:
                raise ValueError("project cache schema version mismatch")
            snapshot: dict[str, list[tuple[Any, ...]]] = {}
            for table in _CACHE_TABLES:
                columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
                order = ", ".join(columns)
                snapshot[table] = connection.execute(
                    f"SELECT {order} FROM {table} ORDER BY {order}"
                ).fetchall()
            return snapshot

    def replay_progress(self) -> list[dict[str, Any]]:
        """Recompute target progress from immutable cached observations."""

        # Only targets and the signals their conditions read can change
        # progress; every other recorded signal is left out of the replay.
        wanted = set()
        for target in self.policy["targets"]:
            wanted.add(str(SignalRef.parse(target["signal_ref"])))
            if target.get("condition") is not None:
                wanted |= condition_refs(target["condition"])
        with closing(connect_cache(self.paths.database)) as connection:
            connection.execute("CREATE TEMP TABLE replay_signal(signal_ref TEXT PRIMARY KEY)")
            connection.executemany("INSERT INTO replay_signal VALUES (?)", ((ref,) for ref in sorted(wanted)))
            observations = []
            for run_id, sequence, signal_ref, raw_value, decoded, elapsed_ms, response_utc in connection.execute(
                "SELECT o.run_id, o.sequence, o.signal_ref, o.raw_value, o.decoded_value, "
                "e.response_elapsed_ms, e.response_utc "
                "FROM signal_observation o JOIN acquisition_exchange e "
                "ON e.run_id = o.run_id AND e.sequence = o.sequence "
                "WHERE o.signal_ref IN (SELECT signal_ref FROM replay_signal) "
                "ORDER BY o.run_id, o.sequence, o.signal_ref"
            ):
                observations.append(
                    ReplayObservation(
                        run_id,
                        sequence,
                        int(elapsed_ms),
                        response_utc,
                        signal_ref,
                        int.from_bytes(raw_value, "big"),
                        decoded,
                    )
                )
            progress = replay_targets(self.policy["targets"], observations)
            connection.execute("DELETE FROM target_progress")
            connection.executemany(
                "INSERT INTO target_progress(signal_ref, document_json) VALUES (?, ?)",
                ((row["signal_ref"], _canonical_json(row)) for row in progress),
            )
            connection.commit()
            return progress

    def acquisition_cost_facts(self) -> dict[str, dict[str, Any]]:
        """Return deterministic observed latency and payload-size facts.

        Positive DID preflight reads are qualification evidence for the same
        endpoint/DID acquisition identity used by planning. They complement
        historical capture exchanges so a newly qualified DID does not need a
        collection run before its first plan can be admitted.
        """

        self._require_cache()
        grouped: dict[str, dict[str, Any]] = {}
        with closing(connect_cache(self.paths.database)) as connection:
            rows = connection.execute(
                "SELECT acquisition_id, response, latency_ms, outcome "
                "FROM acquisition_exchange ORDER BY acquisition_id, run_id, sequence"
            )
            for acquisition_id, response, latency_ms, outcome in rows:
                facts = grouped.setdefault(
                    acquisition_id,
                    {"latencies": [], "maximum_response_bytes": 0, "outcomes": {}},
                )
                facts["latencies"].append(int(latency_ms))
                facts["maximum_response_bytes"] = max(
                    facts["maximum_response_bytes"], len(response) if response is not None else 0
                )
                facts["outcomes"][outcome] = facts["outcomes"].get(outcome, 0) + 1
        for fact in self.did_preflight_facts():
            read = fact["result"]["read"]
            if read is None or read.get("attempted") is not True or read["outcome"] != "positive":
                continue
            acquisition_id = f"{fact['endpoint_id']}/{canonical_did(fact['data_id'])}"
            facts = grouped.setdefault(
                acquisition_id,
                {"latencies": [], "maximum_response_bytes": 0, "outcomes": {}},
            )
            facts["latencies"].append(int(read["latency_ms"]))
            facts["maximum_response_bytes"] = max(
                facts["maximum_response_bytes"], len(bytes.fromhex(read["response_hex"]))
            )
            facts["outcomes"]["positive"] = facts["outcomes"].get("positive", 0) + 1
        result = {}
        for acquisition_id, facts in sorted(grouped.items()):
            ordered = sorted(facts.pop("latencies"))
            index = math.ceil(len(ordered) * 0.95) - 1
            result[acquisition_id] = {
                "sample_count": len(ordered),
                "latency_p95_ms": ordered[max(0, index)],
                **facts,
            }
        return result

    def catalog_variants(self) -> list[dict[str, Any]]:
        with closing(connect_cache(self.paths.database)) as connection:
            return [
                json.loads(document)
                for (document,) in connection.execute(
                    "SELECT document_json FROM catalog_variant ORDER BY signal_ref, catalog_variant_id"
                )
            ]

    def cached_source_hashes(self) -> dict[str, str]:
        with closing(connect_cache(self.paths.database)) as connection:
            return dict(
                connection.execute("SELECT kind, sha256 FROM source_file ORDER BY kind")
            )

    def response_data_bits(self) -> dict[tuple[str, int], int]:
        """Longest positive response data per (endpoint_id, DID), in bits after the 62 DID echo."""

        with closing(connect_cache(self.paths.database)) as connection:
            rows = connection.execute(
                "SELECT acquisition_id, MAX(LENGTH(response)) FROM acquisition_exchange "
                "WHERE outcome = 'positive' GROUP BY acquisition_id"
            ).fetchall()
        result = {}
        for acquisition_id, length in rows:
            ref = AcquisitionRef.parse(acquisition_id)
            result[(ref.endpoint_id, ref.data_id)] = max(0, (int(length) - 3) * 8)
        return result

    def signal_observation_summary(self, run_ids: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
        """Per-signal counts over every run, or only over ``run_ids``."""

        with closing(connect_cache(self.paths.database)) as connection:
            where = ""
            if run_ids is not None:
                connection.execute("CREATE TEMP TABLE wanted_run(run_id TEXT PRIMARY KEY)")
                connection.executemany(
                    "INSERT OR IGNORE INTO wanted_run VALUES (?)", ((run_id,) for run_id in run_ids)
                )
                where = "WHERE run_id IN (SELECT run_id FROM wanted_run) "
            rows = connection.execute(
                "SELECT signal_ref, COUNT(*), COUNT(DISTINCT raw_value), "
                "MIN(decoded_value), MAX(decoded_value) "
                f"FROM signal_observation {where}GROUP BY signal_ref"
            ).fetchall()
        return {
            signal_ref: {
                "observations": count,
                "distinct_raw_values": distinct,
                "decoded_minimum": float(minimum) if minimum is not None else None,
                "decoded_maximum": float(maximum) if maximum is not None else None,
            }
            for signal_ref, count, distinct, minimum, maximum in rows
        }

    def signal_observations(self, signal_ref: str) -> list[dict[str, Any]]:
        """Return every cached observation for one signal across all runs.

        Runs are ordered by their recorded start time (runs without one last),
        so a plot or export reads left to right in run order.
        """

        canonical = str(SignalRef.parse(signal_ref))
        with closing(connect_cache(self.paths.database)) as connection:
            rows = connection.execute(
                "SELECT o.run_id, o.sequence, o.raw_value, o.decoded_value, "
                "o.applicability, e.response_utc, e.response_elapsed_ms "
                "FROM signal_observation o JOIN acquisition_exchange e "
                "ON e.run_id = o.run_id AND e.sequence = o.sequence "
                "JOIN run r ON r.run_id = o.run_id "
                "WHERE o.signal_ref = ? "
                "ORDER BY json_extract(r.document_json, '$.created_utc') IS NULL, "
                "json_extract(r.document_json, '$.created_utc'), o.run_id, o.sequence",
                (canonical,),
            )
            result = []
            for run_id, sequence, raw_value, decoded, applicability, response_utc, elapsed_ms in rows:
                result.append(
                    {
                        "run_id": run_id,
                        "sequence": sequence,
                        "response_utc": response_utc,
                        "response_elapsed_ms": elapsed_ms,
                        "raw_hex": bytes(raw_value).hex(" ").upper(),
                        "decoded_value": decoded,
                        "recorded_applicability": applicability,
                    }
                )
            return result

    def _populate_cache(
        self,
        connection: sqlite3.Connection,
        catalog_rows: list[dict[str, Any]],
        captures_root: Path | None,
    ) -> None:
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            (
                ("cache_schema_version", str(CACHE_SCHEMA_VERSION)),
                ("profile_id", self.profile_id),
                ("project_id", self.project_id),
            ),
        )
        for kind, digest in sorted(self.source_hashes(catalog_rows, captures_root).items()):
            connection.execute(
                "INSERT INTO source_file(kind, sha256) VALUES (?, ?)", (kind, digest)
            )
        self._insert_curated(connection, catalog_rows)
        if captures_root is not None:
            self._insert_capture_runs(connection, captures_root, None, capture_run_hashes(captures_root))
        self._rebuild_did_status(connection)

    def _insert_curated(self, connection: sqlite3.Connection, catalog_rows: list[dict[str, Any]]) -> None:
        for recipe in self.transport["session_recipes"]:
            connection.execute(
                "INSERT INTO session_recipe(id, document_json) VALUES (?, ?)",
                (recipe["id"], _canonical_json(recipe)),
            )
        for endpoint in self.transport["endpoints"]:
            connection.execute(
                "INSERT INTO endpoint(id, tester_can_id, response_can_id, session_recipe_id, provenance_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    endpoint["id"], endpoint["tester_can_id"].upper(),
                    endpoint["response_can_id"].upper(), endpoint["session_recipe_id"],
                    _canonical_json(endpoint["provenance"]),
                ),
            )
            for ecu_key in endpoint["ecu_keys"]:
                connection.execute(
                    "INSERT INTO endpoint_ecu(endpoint_id, ecu_key) VALUES (?, ?)",
                    (endpoint["id"], ecu_key),
                )
        for dynamic in self.transport["dynamic_dids"]:
            connection.execute(
                "INSERT INTO dynamic_did(endpoint_id, target_did, document_json) VALUES (?, ?, ?)",
                (dynamic["endpoint_id"], canonical_did(dynamic["target_did"]), _canonical_json(dynamic)),
            )
        for acquisition in self.policy["acquisitions"]:
            ref = AcquisitionRef.parse(acquisition["acquisition_id"])
            connection.execute(
                "INSERT INTO policy_acquisition(acquisition_id, endpoint_id, data_id, document_json) "
                "VALUES (?, ?, ?, ?)",
                (str(ref), ref.endpoint_id, canonical_did(f"0x{ref.data_id:04X}"), _canonical_json(acquisition)),
            )
        for target in self.policy["targets"]:
            ref = str(SignalRef.parse(target["signal_ref"]))
            connection.execute(
                "INSERT INTO policy_target(signal_ref, document_json) VALUES (?, ?)",
                (ref, _canonical_json(target)),
            )
        for review in self.review["signals"]:
            ref = str(SignalRef.parse(review["signal_ref"]))
            variant = review["catalog_variant_id"] or ""
            connection.execute(
                "INSERT INTO signal_review(signal_ref, catalog_variant_id, document_json) VALUES (?, ?, ?)",
                (ref, variant, _canonical_json(review)),
            )
        for row in catalog_rows:
            connection.execute(
                "INSERT INTO catalog_variant(catalog_variant_id, signal_ref, document_json) VALUES (?, ?, ?)",
                (row["catalog_variant_id"], row["signal_ref"], _canonical_json(row)),
            )

    def _insert_capture_runs(
        self,
        connection: sqlite3.Connection,
        captures_root: Path,
        names: set[str] | None,
        run_hashes: Mapping[str, str],
    ) -> None:
        """Insert the given run folders (all when ``names`` is None)."""

        for run in load_capture_tree(captures_root, self.root, names):
            if run.start["project_id"] != self.project_id or run.start["profile_id"] != self.profile_id:
                raise ValueError(f"capture run {run.run_id} does not belong to this project profile")
            files_sha256 = run_hashes.get(run.run_id, "")
            existing = connection.execute(
                "SELECT document_json FROM run WHERE run_id = ?", (run.run_id,)
            ).fetchone()
            encoded_start = _canonical_json(run.start)
            if existing is not None:
                if existing[0] != encoded_start:
                    raise ValueError(f"capture run identity collision: {run.run_id}")
                connection.execute(
                    "UPDATE run SET files_sha256 = ? WHERE run_id = ?", (files_sha256, run.run_id)
                )
            else:
                connection.execute(
                    "INSERT INTO run(run_id, origin, files_sha256, document_json) VALUES (?, ?, ?, ?)",
                    (run.run_id, run.start["origin"], files_sha256, encoded_start),
                )
            for segment in run.segments:
                existing_segment = connection.execute(
                    "SELECT sha256 FROM segment WHERE run_id = ? AND segment_index = ?",
                    (run.run_id, segment.index),
                ).fetchone()
                if existing_segment is not None:
                    if existing_segment[0] != segment.sha256:
                        raise ValueError(
                            f"capture segment identity collision: {run.run_id}/{segment.index}"
                        )
                    continue
                connection.execute(
                    "INSERT INTO segment(run_id, segment_index, state, path, sha256, document_json) "
                    "VALUES (?, ?, 'complete', ?, ?, ?)",
                    (
                        run.run_id,
                        segment.index,
                        segment.relative_path,
                        segment.sha256,
                        _canonical_json(segment.end),
                    ),
                )
                connection.executemany(
                    "INSERT INTO acquisition_exchange("
                    "run_id, sequence, acquisition_id, endpoint_id, data_id, request, response, outcome, "
                    "negative_response_code, latency_ms, response_elapsed_ms, response_utc, document_json"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            run.run_id,
                            exchange["run_sequence"],
                            exchange["acquisition_id"],
                            exchange["endpoint_id"],
                            exchange["data_id"],
                            bytes.fromhex(exchange["request_hex"]),
                            None if exchange["response_hex"] is None else bytes.fromhex(exchange["response_hex"]),
                            exchange["outcome"],
                            exchange["negative_response_code"],
                            exchange["latency_ms"],
                            exchange["response_elapsed_ms"],
                            exchange["response_utc"],
                            _canonical_json(exchange),
                        )
                        for exchange in segment.exchanges
                    ],
                )
                connection.executemany(
                    "INSERT INTO signal_observation("
                    "run_id, sequence, signal_ref, raw_value, decoded_value, applicability"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            run.run_id,
                            exchange["run_sequence"],
                            observation["signal_ref"],
                            bytes.fromhex(observation["raw_hex"]),
                            observation["decoded_value"],
                            observation["applicability"],
                        )
                        for exchange in segment.exchanges
                        for observation in exchange["observations"]
                    ],
                )

    @staticmethod
    def _rebuild_did_status(connection: sqlite3.Connection) -> None:
        grouped: dict[tuple[str, str], dict[str, int]] = {}
        for endpoint_id, data_id, outcome, code in connection.execute(
            "SELECT endpoint_id, data_id, outcome, negative_response_code "
            "FROM acquisition_exchange ORDER BY run_id, sequence"
        ):
            label = "supported" if outcome == "positive" else str(code or "negative")
            counts = grouped.setdefault((endpoint_id, data_id), {})
            counts[label] = counts.get(label, 0) + 1
        connection.execute("DELETE FROM did_status")
        connection.executemany(
            "INSERT INTO did_status(endpoint_id, data_id, status, document_json) VALUES (?, ?, ?, ?)",
            [
                (
                    endpoint_id,
                    data_id,
                    next(iter(counts)) if len(counts) == 1 else "conflicting",
                    _canonical_json({"authority": "provisional_legacy_import", "counts": counts}),
                )
                for (endpoint_id, data_id), counts in sorted(grouped.items())
            ],
        )


def _normalized_catalog_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for index, raw in enumerate(rows):
        try:
            signal_ref = str(SignalRef.parse(str(raw["signal_ref"])))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"catalog row {index} has invalid signal_ref") from exc
        document = dict(raw)
        document["signal_ref"] = signal_ref
        variant = document.get("catalog_variant_id")
        if variant is None:
            seed = {key: value for key, value in document.items() if key != "catalog_variant_id"}
            variant = _sha256(_canonical_json(seed).encode("utf-8"))[:16]
        if not isinstance(variant, str) or not variant:
            raise ValueError(f"catalog row {index} has invalid catalog_variant_id")
        document["catalog_variant_id"] = variant
        encoded = _canonical_json(document)
        if variant in seen:
            if seen[variant] == encoded:
                continue
            raise ValueError(f"conflicting catalog_variant_id: {variant}")
        seen[variant] = encoded
        normalized.append(document)
    return sorted(normalized, key=lambda row: (row["signal_ref"], row["catalog_variant_id"]))


def _create_cache_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE source_file (
            kind TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL CHECK(length(sha256) = 64)
        );
        CREATE TABLE session_recipe (
            id TEXT PRIMARY KEY,
            document_json TEXT NOT NULL
        );
        CREATE TABLE endpoint (
            id TEXT PRIMARY KEY,
            tester_can_id TEXT NOT NULL,
            response_can_id TEXT NOT NULL,
            session_recipe_id TEXT NOT NULL REFERENCES session_recipe(id),
            provenance_json TEXT NOT NULL
        );
        CREATE TABLE endpoint_ecu (
            endpoint_id TEXT NOT NULL REFERENCES endpoint(id),
            ecu_key INTEGER NOT NULL UNIQUE,
            PRIMARY KEY(endpoint_id, ecu_key)
        );
        CREATE TABLE dynamic_did (
            endpoint_id TEXT NOT NULL REFERENCES endpoint(id),
            target_did TEXT NOT NULL,
            document_json TEXT NOT NULL,
            PRIMARY KEY(endpoint_id, target_did)
        );
        CREATE TABLE policy_acquisition (
            acquisition_id TEXT PRIMARY KEY,
            endpoint_id TEXT NOT NULL REFERENCES endpoint(id),
            data_id TEXT NOT NULL,
            document_json TEXT NOT NULL
        );
        CREATE TABLE policy_target (
            signal_ref TEXT PRIMARY KEY,
            document_json TEXT NOT NULL
        );
        CREATE TABLE signal_review (
            signal_ref TEXT NOT NULL,
            catalog_variant_id TEXT NOT NULL,
            document_json TEXT NOT NULL,
            PRIMARY KEY(signal_ref, catalog_variant_id)
        );
        CREATE TABLE catalog_variant (
            catalog_variant_id TEXT PRIMARY KEY,
            signal_ref TEXT NOT NULL,
            document_json TEXT NOT NULL
        );
        CREATE TABLE run (
            run_id TEXT PRIMARY KEY,
            origin TEXT NOT NULL,
            files_sha256 TEXT NOT NULL,
            document_json TEXT NOT NULL
        );
        CREATE TABLE segment (
            run_id TEXT NOT NULL REFERENCES run(run_id),
            segment_index INTEGER NOT NULL,
            state TEXT NOT NULL,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            document_json TEXT NOT NULL,
            PRIMARY KEY(run_id, segment_index)
        );
        CREATE TABLE acquisition_exchange (
            run_id TEXT NOT NULL REFERENCES run(run_id),
            sequence INTEGER NOT NULL,
            acquisition_id TEXT NOT NULL,
            endpoint_id TEXT NOT NULL,
            data_id TEXT NOT NULL,
            request BLOB NOT NULL,
            response BLOB,
            outcome TEXT NOT NULL,
            negative_response_code,
            latency_ms INTEGER,
            response_elapsed_ms INTEGER NOT NULL,
            response_utc TEXT,
            document_json TEXT NOT NULL,
            PRIMARY KEY(run_id, sequence)
        );
        CREATE TABLE signal_observation (
            run_id TEXT NOT NULL REFERENCES run(run_id),
            sequence INTEGER NOT NULL,
            signal_ref TEXT NOT NULL,
            raw_value BLOB NOT NULL,
            decoded_value REAL,
            applicability TEXT NOT NULL,
            PRIMARY KEY(run_id, sequence, signal_ref),
            FOREIGN KEY(run_id, sequence) REFERENCES acquisition_exchange(run_id, sequence)
        );
        CREATE INDEX signal_observation_by_signal ON signal_observation(signal_ref);
        CREATE TABLE did_status (
            endpoint_id TEXT NOT NULL REFERENCES endpoint(id),
            data_id TEXT NOT NULL,
            status TEXT NOT NULL,
            document_json TEXT NOT NULL,
            PRIMARY KEY(endpoint_id, data_id)
        );
        CREATE TABLE target_progress (
            signal_ref TEXT PRIMARY KEY REFERENCES policy_target(signal_ref),
            document_json TEXT NOT NULL
        );
        CREATE TABLE export_generation (
            generation_id TEXT PRIMARY KEY,
            created_utc TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            document_json TEXT NOT NULL
        );
        """
    )
