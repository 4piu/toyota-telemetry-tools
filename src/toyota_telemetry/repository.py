"""Canonical project repository and derived SQLite query cache."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import tomllib
from typing import Any, Iterable, Mapping

from .identity import AcquisitionRef, SignalRef, canonical_did
from .schemas import (
    validate_local_settings,
    validate_policy,
    validate_project_marker,
    validate_review,
    validate_transport,
)


CACHE_SCHEMA_VERSION = 1
_CACHE_TABLES = (
    "schema_meta",
    "source_file",
    "session_recipe",
    "endpoint",
    "endpoint_ecu",
    "dynamic_did",
    "policy_class_default",
    "policy_acquisition",
    "policy_objective",
    "signal_review",
    "catalog_variant",
    "run",
    "segment",
    "acquisition_exchange",
    "signal_observation",
    "did_status",
    "objective_progress",
    "export_generation",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    transport: Path
    collection_policy: Path
    signal_review: Path
    catalog_manifest: Path
    result: Path
    scratch: Path
    local_settings: Path
    database: Path
    captures: Path
    plans: Path
    toolkit: Path


class ProjectRepository:
    """Validated schema-v2 project accessed through one repository boundary."""

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
        self.paths = self._resolve_paths(marker["paths"])

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

    def _resolve_paths(self, values: Mapping[str, str]) -> ProjectPaths:
        resolved: dict[str, Path] = {}
        for name, value in values.items():
            raw = Path(value)
            path = raw.resolve() if raw.is_absolute() else (self.root / raw).resolve()
            if name != "toolkit" and not path.is_relative_to(self.root):
                raise ValueError(
                    f"telemetry-project.toml paths.{name} escapes the project root"
                )
            resolved[name] = path
        return ProjectPaths(**resolved)

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

        for row in self.policy["objectives"]:
            self._validate_signal_ref(row["signal_ref"], ecu_to_endpoint, did_min, did_max)
            if row["applicability"] is not None:
                for signal_ref in _applicability_refs(row["applicability"]):
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

    def source_hashes(self, catalog_rows: Iterable[Mapping[str, Any]] = ()) -> dict[str, str]:
        rows = _normalized_catalog_rows(catalog_rows)
        hashes = {
            "transport": _sha256(self._transport_bytes),
            "collection_policy": _sha256(self._policy_bytes),
            "signal_review": _sha256(self._review_bytes),
            "catalog_manifest": _sha256(self._catalog_manifest_bytes),
            "catalog_rows": _sha256((_canonical_json(rows) + "\n").encode("utf-8")),
        }
        return hashes

    def rebuild_cache(self, catalog_rows: Iterable[Mapping[str, Any]] = ()) -> None:
        rows = _normalized_catalog_rows(catalog_rows)
        self._validate_catalog_rows(rows)
        database = self.paths.database
        database.parent.mkdir(parents=True, exist_ok=True)
        temporary = database.with_suffix(database.suffix + ".tmp")
        if temporary.exists():
            temporary.unlink()
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            _create_cache_schema(connection)
            self._populate_cache(connection, rows)
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

    def refresh_cache(self, catalog_rows: Iterable[Mapping[str, Any]] = ()) -> bool:
        rows = _normalized_catalog_rows(catalog_rows)
        self._validate_catalog_rows(rows)
        if self.paths.database.is_file():
            try:
                with closing(sqlite3.connect(self.paths.database)) as connection:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    current = dict(connection.execute("SELECT kind, sha256 FROM source_file"))
                if version == CACHE_SCHEMA_VERSION and current == self.source_hashes(rows):
                    return False
            except sqlite3.Error:
                pass
        self.rebuild_cache(rows)
        return True

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

        required_refs = {row["signal_ref"] for row in self.policy["objectives"]}
        required_refs.update(row["signal_ref"] for row in self.review["signals"])
        for objective in self.policy["objectives"]:
            if objective["applicability"] is not None:
                required_refs.update(_applicability_refs(objective["applicability"]))
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
        with closing(sqlite3.connect(self.paths.database)) as connection:
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

    def _populate_cache(
        self, connection: sqlite3.Connection, catalog_rows: list[dict[str, Any]]
    ) -> None:
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            (
                ("cache_schema_version", str(CACHE_SCHEMA_VERSION)),
                ("profile_id", self.profile_id),
                ("project_id", self.project_id),
            ),
        )
        for kind, digest in sorted(self.source_hashes(catalog_rows).items()):
            connection.execute(
                "INSERT INTO source_file(kind, sha256) VALUES (?, ?)", (kind, digest)
            )
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
        for name, rule in self.policy["class_defaults"].items():
            connection.execute(
                "INSERT INTO policy_class_default(class, document_json) VALUES (?, ?)",
                (name, _canonical_json(rule)),
            )
        for acquisition in self.policy["acquisitions"]:
            ref = AcquisitionRef.parse(acquisition["acquisition_id"])
            connection.execute(
                "INSERT INTO policy_acquisition(acquisition_id, endpoint_id, data_id, document_json) "
                "VALUES (?, ?, ?, ?)",
                (str(ref), ref.endpoint_id, canonical_did(f"0x{ref.data_id:04X}"), _canonical_json(acquisition)),
            )
        for objective in self.policy["objectives"]:
            ref = str(SignalRef.parse(objective["signal_ref"]))
            connection.execute(
                "INSERT INTO policy_objective(signal_ref, objective_id, document_json) VALUES (?, ?, ?)",
                (ref, objective["objective_id"], _canonical_json(objective)),
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


def _applicability_refs(node: Mapping[str, Any]) -> Iterable[str]:
    if "all" in node:
        for child in node["all"]:
            yield from _applicability_refs(child)
    elif "any" in node:
        for child in node["any"]:
            yield from _applicability_refs(child)
    else:
        yield node["signal_ref"]


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
        CREATE TABLE policy_class_default (
            class TEXT PRIMARY KEY,
            document_json TEXT NOT NULL
        );
        CREATE TABLE policy_acquisition (
            acquisition_id TEXT PRIMARY KEY,
            endpoint_id TEXT NOT NULL REFERENCES endpoint(id),
            data_id TEXT NOT NULL,
            document_json TEXT NOT NULL
        );
        CREATE TABLE policy_objective (
            signal_ref TEXT NOT NULL,
            objective_id TEXT NOT NULL,
            document_json TEXT NOT NULL,
            PRIMARY KEY(signal_ref, objective_id)
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
            request BLOB NOT NULL,
            response BLOB,
            outcome TEXT NOT NULL,
            latency_ms INTEGER,
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
        CREATE TABLE did_status (
            endpoint_id TEXT NOT NULL REFERENCES endpoint(id),
            data_id TEXT NOT NULL,
            status TEXT NOT NULL,
            document_json TEXT NOT NULL,
            PRIMARY KEY(endpoint_id, data_id)
        );
        CREATE TABLE objective_progress (
            signal_ref TEXT NOT NULL,
            objective_id TEXT NOT NULL,
            document_json TEXT NOT NULL,
            PRIMARY KEY(signal_ref, objective_id),
            FOREIGN KEY(signal_ref, objective_id)
                REFERENCES policy_objective(signal_ref, objective_id)
        );
        CREATE TABLE export_generation (
            generation_id TEXT PRIMARY KEY,
            created_utc TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            document_json TEXT NOT NULL
        );
        """
    )
