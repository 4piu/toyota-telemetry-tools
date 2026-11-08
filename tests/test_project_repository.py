from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from toyota_telemetry.identity import AcquisitionRef, SignalRef, canonical_run_id
from toyota_telemetry.repository import CACHE_SCHEMA_VERSION, ProjectRepository
from toyota_telemetry.schemas import validate_local_settings, validate_transport


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def transport() -> dict:
    return {
        "document_type": "telemetry.transport",
        "schema_version": 1,
        "profile_id": "test-profile",
        "network": {
            "type": "classical-can",
            "bitrate": 500000,
            "addressing": "normal",
        },
        "safety": {
            "did_min": "0x0100",
            "did_max": "0xEFFF",
            "request_start_floor_ms": 100,
            "maximum_request_rate_hz": 10,
            "maximum_estimated_bus_utilization": 0.05,
            "maximum_nominal_session_utilization": 0.5,
            "initial_response_timeout_ms": 1000,
            "response_pending_budget_ms": 10000,
            "transport_retries": 2,
        },
        "session_recipes": [
            {
                "id": "default-extended",
                "steps": [
                    {
                        "request_hex": "10 03",
                        "expected_prefix_hex": "50 03",
                        "label": "extended session",
                        "tolerate_negative": False,
                    }
                ],
            }
        ],
        "endpoints": [
            {
                "id": "700-708-extended",
                "tester_can_id": "0x700",
                "response_can_id": "0x708",
                "session_recipe_id": "default-extended",
                "ecu_keys": [372],
                "state": "confirmed",
                "provenance": ["docs/transport.md"],
            }
        ],
        "dynamic_dids": [],
    }


def policy() -> dict:
    return {
        "document_type": "telemetry.collection-policy",
        "schema_version": 2,
        "profile_id": "test-profile",
        "storage": {
            "segment_seconds": 60,
            "segment_max_mib": 16,
            "capture_tree_max_mib": 2048,
            "disk_reserve_mib": 256,
        },
        "acquisitions": [
            {
                "acquisition_id": "700-708-extended/0x1002",
                "included": True,
                "priority": 50,
                "target_interval_ms": 100,
                "maximum_interval_ms": 250,
            }
        ],
        "targets": [
            {
                "signal_ref": "372/0x1002/0..15",
                "included": True,
                "minimum_difference": 0.1,
                "requirements": {
                    "minimum_changes": 10,
                    "minimum_applicable_seconds": 2,
                    "minimum_runs": 1,
                },
                "condition": {
                    "signal_ref": "372/0x1002/0..15",
                    "operator": "gte",
                    "value": 0,
                    "maximum_age_ms": 500,
                },
            }
        ],
    }


def review() -> dict:
    return {
        "document_type": "telemetry.signal-review",
        "schema_version": 1,
        "profile_id": "test-profile",
        "signals": [
            {
                "signal_ref": "372/0x1002/0..15",
                "catalog_variant_id": None,
                "display_name_override": None,
                "formula_override": None,
                "unit_override": None,
                "evidence_state": "Candidate",
                "notes": "",
                "evidence_refs": [],
            }
        ],
    }


def make_project(root: Path) -> None:
    root.joinpath("telemetry-project.toml").write_text(
        "\n".join(
            [
                "schema_version = 3",
                'project_id = "test-project"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_json(root / "config/transport.json", transport())
    write_json(root / "config/collection-policy.json", policy())
    write_json(root / "review/signals.json", review())
    write_json(root / "config/catalog-manifest.json", {"schema_version": 1})


class IdentityTests(unittest.TestCase):
    def test_canonical_identities(self) -> None:
        self.assertEqual(str(SignalRef.parse("397/0x1f9a/32..47")), "397/0x1F9A/32..47")
        self.assertEqual(
            str(AcquisitionRef.parse("7d2-7da-extended/0x1f9a")),
            "7d2-7da-extended/0x1F9A",
        )
        self.assertEqual(
            canonical_run_id("123e4567-e89b-12d3-a456-426614174000"),
            "123e4567-e89b-12d3-a456-426614174000",
        )
        with self.assertRaises(ValueError):
            SignalRef.parse("397/0x1F9A/47..32")
        with self.assertRaises(ValueError):
            canonical_run_id("123E4567-E89B-12D3-A456-426614174000")


class SchemaTests(unittest.TestCase):
    def test_unknown_transport_field_is_rejected(self) -> None:
        document = transport()
        document["fallback"] = True
        with self.assertRaisesRegex(ValueError, "unknown fields: fallback"):
            validate_transport(document)

    def test_unreviewed_dynamic_did_is_rejected(self) -> None:
        document = transport()
        document["dynamic_dids"] = [
            {
                "endpoint_id": "700-708-extended",
                "target_did": "0xF301",
                "definition_hex": "2C 01 F3 01 10 A0 01 02",
                "expected_prefix_hex": "6C 01 F3 01",
                "state": "candidate",
                "provenance": ["docs/transport.md"],
            }
        ]
        with self.assertRaisesRegex(ValueError, "reviewed_replay"):
            validate_transport(document)

    def test_session_service_outside_allowlist_is_rejected(self) -> None:
        document = transport()
        document["session_recipes"][0]["steps"][0]["request_hex"] = "27 01"
        with self.assertRaisesRegex(ValueError, "service is not allowlisted"):
            validate_transport(document)

    def test_local_settings_use_exact_backend_schema(self) -> None:
        document = {
            "document_type": "telemetry.local-settings",
            "schema_version": 1,
            "project_id": "test-project",
            "backend": {
                "kind": "pcan",
                "executable": "state/bin/telemetry-pcan.exe",
                "settings": {"channel": "PCAN_USBBUS1"},
            },
        }
        validate_local_settings(document)
        document["backend"]["settings"]["fallback_channel"] = "PCAN_USBBUS2"
        with self.assertRaisesRegex(ValueError, "unknown fields: fallback_channel"):
            validate_local_settings(document)


class RepositoryTests(unittest.TestCase):
    def test_cache_rebuild_is_deterministic_and_refresh_is_idempotent(self) -> None:
        catalog = [
            {
                "signal_ref": "372/0x1002/0..15",
                "name": "Battery voltage",
                "formula": "raw / 1000",
                "unit": "V",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = ProjectRepository(root)
            repository.rebuild_cache(catalog)
            first = repository.cache_snapshot()
            self.assertEqual(first["schema_meta"][0], ("cache_schema_version", str(CACHE_SCHEMA_VERSION)))
            self.assertEqual(len(first["catalog_variant"]), 1)
            self.assertEqual(first["run"], [])
            self.assertFalse(repository.refresh_cache(catalog))

            repository.paths.database.unlink()
            repository.rebuild_cache(catalog)
            self.assertEqual(first, repository.cache_snapshot())

            changed_review = review()
            changed_review["signals"][0]["notes"] = "Reviewed later."
            write_json(root / "review/signals.json", changed_review)
            repository = ProjectRepository(root)
            self.assertTrue(repository.refresh_cache(catalog))
            self.assertIn("Reviewed later.", repository.cache_snapshot()["signal_review"][0][-1])

    def test_authored_writer_is_exact_atomic_and_marks_cache_stale(self) -> None:
        catalog = [{"signal_ref": "372/0x1002/0..15", "name": "Battery voltage"}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = ProjectRepository(root)
            repository.rebuild_cache(catalog)

            changed = review()
            changed["signals"][0]["notes"] = "Atomic review."
            repository.write_authored_document("signal_review", changed)
            data = repository.paths.signal_review.read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
            self.assertTrue(data.endswith(b"\n"))
            self.assertFalse(data.endswith(b"\n\n"))
            self.assertEqual(json.loads(data), changed)
            self.assertTrue(repository.refresh_cache(catalog))

            invalid = dict(changed)
            invalid["unexpected"] = True
            with self.assertRaisesRegex(ValueError, "unknown fields: unexpected"):
                repository.write_authored_document("signal_review", invalid)
            self.assertEqual(repository.paths.signal_review.read_bytes(), data)

    def test_equivalent_catalog_variants_coalesce(self) -> None:
        row = {
            "signal_ref": "372/0x1002/0..15",
            "name": "Battery voltage",
            "formula": "raw / 1000",
            "unit": "V",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = ProjectRepository(root)
            repository.rebuild_cache([row, dict(row)])
            self.assertEqual(len(repository.cache_snapshot()["catalog_variant"]), 1)

    def test_conflicting_catalog_variants_require_review_resolution(self) -> None:
        # Two versions (formulas) of one reviewed signal that also disagree on the name.
        rows = [
            {"signal_ref": "372/0x1002/0..15", "name": name, "unit": "V", "formula": formula}
            for name, formula in (("Battery voltage", "raw / 1000"), ("Supply voltage", "raw / 100"))
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = ProjectRepository(root)
            with self.assertRaisesRegex(ValueError, "catalog variants conflict.*name"):
                repository.rebuild_cache(rows)

    def test_unresolved_policy_reference_fails_before_cache_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            document = policy()
            document["targets"][0]["signal_ref"] = "999/0x1002/0..15"
            write_json(root / "config/collection-policy.json", document)
            with self.assertRaisesRegex(ValueError, "unresolved signal_ref ECU"):
                ProjectRepository(root)
            self.assertFalse((root / "state/project.sqlite").exists())

    def test_profile_ids_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            document = review()
            document["profile_id"] = "other-profile"
            write_json(root / "review/signals.json", document)
            with self.assertRaisesRegex(ValueError, "profile_id values must match"):
                ProjectRepository(root)

    def test_old_project_marker_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            marker = root / "telemetry-project.toml"
            marker.write_text(marker.read_text(encoding="utf-8").replace(
                "schema_version = 3", "schema_version = 2"
            ), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema_version must be 3"):
                ProjectRepository(root)


if __name__ == "__main__":
    unittest.main()
