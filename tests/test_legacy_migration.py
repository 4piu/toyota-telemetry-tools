from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from toyota_telemetry.legacy_migration import convert_legacy_project
from toyota_telemetry.repository import ProjectRepository


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def make_legacy_project(root: Path) -> Path:
    root.joinpath("telemetry-project.toml").write_text(
        '\n'.join([
            'schema_version = 1',
            'project_kind = "vehicle"',
            'project_id = "test-project"',
            '',
            '[upstream]',
            'repository = "https://example.invalid/tools.git"',
            'revision = "main"',
            '',
            '[paths]',
            'scratch = "scratch"',
            'toolkit = "../telemetry-tools"',
            '',
        ]),
        encoding="utf-8",
    )
    profile = {
        "schema_version": 2,
        "profile_id": "test-profile",
        "transport": {"bitrate": 500000, "addressing": "normal"},
        "catalog": {"path": "data/catalog/telemetry.csv", "acquisition": "direct DID"},
        "selection": {
            "did_min": "0x0100",
            "did_max": "0xEFFF",
            "maximum_dids_per_road_batch": 12,
            "default_tier": "normal",
            "tier_words": {"fast": ["voltage"], "slow": [], "archive": []},
        },
        "policies": {
            "fast": {"period_ms": 100, "duration_seconds": 2, "minimum_samples_per_did": 1, "maximum_dids_per_batch": 2},
            "normal": {"period_ms": 250, "duration_seconds": 2, "minimum_samples_per_did": 1, "maximum_dids_per_batch": 2},
        },
        "backend": {"executable": "scratch/bin/telemetry-pcan.exe"},
        "ecus": [{
            "key": 372,
            "name": "Engine",
            "plan_id": "engine-v1",
            "tester_can_id": "0x700",
            "response_can_id": "0x708",
            "paths": {
                "plan": "scratch/plans/engine.json",
                "capture_root": "scratch/captures/engine",
            },
        }],
    }
    write_json(root / "data/config/validation-profile.json", profile)
    write_json(root / "data/config/backend-config.json", {
        "schema_version": 2,
        "profile_id": "test-profile",
        "transport": {"network": "classical-can", "bitrate": 500000, "addressing": "normal"},
        "session_setup": [{"request": "10 03", "expect": "50 03", "label": "extended"}],
        "limits": {"maximum_candidate_dids": 12, "did_min": "0x0100", "did_max": "0xEFFF", "response_pending_budget_ms": 10000},
        "device": {"pcan": {"channel": "PCAN_USBBUS1"}},
        "ecus": [{"key": 372, "name": "Engine", "tester_can_id": "0x700", "response_can_id": "0x708"}],
    })
    write_json(root / "data/map/transport.json", {"schema_version": 2, "dynamic_dids": []})
    write_json(root / "data/config/ecu-profile.json", {"schema_version": 1})
    write_json(root / "data/catalog/manifest.json", {"schema_version": 1})
    write_json(root / "data/config/decode-overrides.json", {
        "schema_version": 1,
        "profile_id": "test-profile",
        "signals": [{
            "ecu_key": 372,
            "data_id": "0x1002",
            "name": "Battery voltage",
            "first_bit": 0,
            "last_bit": 15,
            "formula": "raw / 1000",
            "unit": "V",
            "evidence_state": "Observed and replayed",
            "notes": "Reviewed decode.",
        }],
    })
    write_csv(
        root / "data/map/signals.csv",
        ["ecu_key", "data_id", "first_bit", "last_bit", "name", "conversion", "unit", "internal_id"],
        [{"ecu_key": 372, "data_id": "0x1002", "first_bit": 0, "last_bit": 15, "name": "Battery voltage", "conversion": "raw / 1000", "unit": "V", "internal_id": "0x0001"}],
    )
    write_csv(
        root / "data/config/curation.csv",
        ["ecu_key", "data_id", "first_bit", "last_bit", "name", "evidence_state", "notes"],
        [{"ecu_key": 372, "data_id": "0x1002", "first_bit": 0, "last_bit": 15, "name": "Battery voltage", "evidence_state": "Observed and replayed", "notes": "Reviewed decode."}],
    )
    write_csv(
        root / "data/map/unsupported.csv",
        ["ecu_key", "data_id", "result"],
        [],
    )

    capture = root / "scratch/captures/engine/one.complete.jsonl"
    capture.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"record": "session_start", "schema_version": 1, "profile_id": "test-profile", "mode": "candidate_qualification", "batch_id": "one", "transport": {"backend": "synthetic", "bitrate": 500000, "tester_id": "0x700", "response_id": "0x708"}, "dids": ["0x1002"], "period_ms": 100},
        {"record": "candidate_sample", "sequence": 1, "did": "0x1002", "request_elapsed_ms": 100, "response_elapsed_ms": 110, "latency_ms": 10, "request_hex": "22 10 02", "response_hex": "62 10 02 34 12", "retry_count": 0, "supported": True, "qualification_status": "supported", "negative_response_code": None},
        {"record": "session_end", "reason": "completed", "sample_count": 1, "did_counts": {"0x1002": 1}},
    ]
    capture.write_bytes(b"".join((json.dumps(row, separators=(",", ":")) + "\n").encode() for row in records))
    rejected = root / "scratch/captures/engine/old-road.complete.jsonl"
    rejected_records = [
        {"record": "session_start", "schema_version": 1, "profile_id": "test-profile", "mode": "road_capture", "transport": {"backend": "synthetic", "bitrate": 500000, "tester_id": "0x700", "response_id": "0x708"}, "dids": ["0x1002"]},
        {"record": "session_end", "reason": "completed", "sample_count": 0, "did_counts": {}},
    ]
    rejected.write_bytes(b"".join((json.dumps(row, separators=(",", ":")) + "\n").encode() for row in rejected_records))
    return capture


class LegacyMigrationTests(unittest.TestCase):
    def test_converter_acceptance_contract_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            source_capture = make_legacy_project(root)
            source_before = tree_hashes(root)
            destination = root / "scratch/migration-v1"

            report = convert_legacy_project(root, destination, "deadbeef")
            self.assertEqual(report["capture_counts"]["discovered"], 2)
            self.assertEqual(report["capture_counts"]["converted"], 1)
            self.assertEqual(report["capture_counts"]["rejected"], 1)
            self.assertEqual(report["rejection_reasons"], {"unsupported_legacy_mode": 1})
            self.assertTrue(report["old_sources_unchanged"])
            self.assertTrue(report["cache_equivalence_verified"])
            self.assertTrue(report["review_required"])
            self.assertEqual(report["did_reconciliation"]["discrepancy_count"], 0)
            self.assertEqual(report["identity_counts"]["curation_rows"], 1)
            self.assertEqual(report["identity_counts"]["decode_overrides"], 1)

            converted_review = json.loads((destination / "review/signals.json").read_text())
            self.assertEqual(converted_review["signals"][0]["formula_override"], "raw / 1000")
            converted = next(item for item in report["captures"] if item["status"] == "converted")
            self.assertEqual(hashlib.sha256(source_capture.read_bytes()).hexdigest(), converted["source_sha256"])
            for relative, digest in report["authored_output_hashes"].items():
                self.assertEqual(hashlib.sha256((destination / relative).read_bytes()).hexdigest(), digest)
            for relative, digest in converted["output_hashes"].items():
                output = destination / "captures" / converted["run_id"] / relative
                self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), digest)
            ProjectRepository(destination).cache_snapshot()

            source_after = {key: value for key, value in tree_hashes(root).items() if not key.startswith("scratch/migration-v1/")}
            self.assertEqual(source_before, source_after)
            output_before = tree_hashes(destination)
            second = convert_legacy_project(root, destination, "deadbeef")
            self.assertEqual(report, second)
            self.assertEqual(output_before, tree_hashes(destination))

    def test_destination_must_not_overlap_legacy_captures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vehicle"
            root.mkdir()
            make_legacy_project(root)
            with self.assertRaisesRegex(ValueError, "must not overlap"):
                convert_legacy_project(root, root / "scratch/captures/migration", "deadbeef")


if __name__ == "__main__":
    unittest.main()
