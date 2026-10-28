from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from toyota_telemetry.discovery import (
    JsonlProbeClient,
    discover_endpoints,
    preflight_dids,
    promote_confirmed_endpoint,
)
from toyota_telemetry.repository import ProjectRepository
from toyota_telemetry.schemas import validate_transport


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def transport() -> dict:
    return {
        "document_type": "telemetry.transport",
        "schema_version": 1,
        "profile_id": "test-profile",
        "network": {"type": "classical-can", "bitrate": 500000, "addressing": "normal"},
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
                "id": "extended",
                "steps": [
                    {
                        "request_hex": "3E 00",
                        "expected_prefix_hex": "7E 00",
                        "label": "tester present",
                        "tolerate_negative": True,
                    },
                    {
                        "request_hex": "10 03",
                        "expected_prefix_hex": "50 03",
                        "label": "extended session",
                        "tolerate_negative": False,
                    },
                ],
            }
        ],
        "endpoints": [
            {
                "id": "700-708-extended",
                "tester_can_id": "0x700",
                "response_can_id": "0x708",
                "session_recipe_id": "extended",
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
        "acquisitions": [],
        "targets": [],
    }


def review() -> dict:
    return {
        "document_type": "telemetry.signal-review",
        "schema_version": 1,
        "profile_id": "test-profile",
        "signals": [],
    }


def local_settings() -> dict:
    return {
        "document_type": "telemetry.local-settings",
        "schema_version": 1,
        "project_id": "test-project",
        "backend": {
            "kind": "pcan",
            "executable": "scratch/bin/telemetry-pcan.exe",
            "settings": {"channel": "PCAN_USBBUS1"},
        },
    }


def make_project(root: Path) -> None:
    root.joinpath("telemetry-project.toml").write_text(
        "\n".join(
            [
                "schema_version = 2",
                'project_kind = "vehicle"',
                'project_id = "test-project"',
                "",
                "[toolkit]",
                'repository = "https://example.invalid/tools.git"',
                'revision = "main"',
                "",
                "[paths]",
                'transport = "config/transport.json"',
                'collection_policy = "config/collection-policy.json"',
                'signal_review = "review/signals.json"',
                'catalog_manifest = "result/catalog-manifest.json"',
                'result = "result"',
                'scratch = "scratch"',
                'local_settings = "scratch/local-settings.json"',
                'database = "scratch/project.sqlite"',
                'captures = "scratch/captures"',
                'plans = "scratch/plans"',
                'toolkit = "../telemetry-tools"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_json(root / "config/transport.json", transport())
    write_json(root / "config/collection-policy.json", policy())
    write_json(root / "review/signals.json", review())
    write_json(root / "result/catalog-manifest.json", {"schema_version": 1})
    write_json(root / "scratch/local-settings.json", local_settings())


def make_repository(root: Path) -> ProjectRepository:
    """Construct a repository with a rebuilt cache, matching the real
    operator order (catalog import / cache rebuild before any live probe)."""

    repository = ProjectRepository(root)
    repository.rebuild_cache([], repository.paths.captures)
    return repository


def probe_result(*, responded: bool, read_outcome: str | None = None) -> dict:
    result = {
        "event": "probe_result",
        "schema_version": 1,
        "candidate_id": "irrelevant-for-tests",
        "backend_kind": "synthetic",
        "responded": responded,
        "session_steps": [
            {
                "label": "tester present",
                "outcome": "positive" if responded else "timeout",
                "latency_ms": 5,
                "nrc": None,
                "response_hex": "7E 00" if responded else None,
            }
        ],
        "read": None,
    }
    if read_outcome is not None:
        result["read"] = {
            "attempted": read_outcome != "session_unavailable",
            "outcome": read_outcome,
            "latency_ms": 8,
            "nrc": "0x31" if read_outcome == "negative" else None,
            "response_hex": "62 10 75 00 12" if read_outcome == "positive" else None,
        }
        if read_outcome == "session_unavailable":
            result["read"] = {"attempted": False, "outcome": "session_unavailable"}
    return result


class ScriptedProbeWorker:
    def __init__(self, result: dict) -> None:
        self.result = result

    def probe(self) -> dict:
        return self.result


def scripted_factory(results: list[dict]):
    queue = list(results)

    def factory(config_path: Path):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        result = queue.pop(0)
        result["candidate_id"] = config["candidate_id"]
        result["backend_kind"] = next(iter(config["device"]))
        return ScriptedProbeWorker(result)

    return factory


class DiscoveryTests(unittest.TestCase):
    def test_discover_endpoints_records_responding_and_silent_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)
            factory = scripted_factory([
                probe_result(responded=True),
                probe_result(responded=False),
            ])
            results = discover_endpoints(
                repository, root,
                [("0x700", "0x708", "extended"), ("0x701", "0x709", "extended")],
                utc_now=lambda: "2026-01-01T00:00:00Z",
                probe_worker_factory=factory,
            )
            self.assertEqual([row["responded"] for row in results], [True, False])
            stored = repository.discovery_candidates()
            self.assertEqual(len(stored), 2)
            self.assertEqual(stored[0]["candidate_id"], "700-708-extended")
            self.assertTrue(stored[0]["responded"])
            self.assertEqual(stored[1]["candidate_id"], "701-709-extended")
            self.assertFalse(stored[1]["responded"])

    def test_candidate_identity_includes_the_session_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            document = transport()
            document["session_recipes"].append({
                "id": "default",
                "steps": [{
                    "request_hex": "10 01",
                    "expected_prefix_hex": "50 01",
                    "label": "default session",
                    "tolerate_negative": False,
                }],
            })
            write_json(root / "config/transport.json", document)
            repository = make_repository(root)
            discover_endpoints(
                repository, root,
                [("0x700", "0x708", "extended"), ("0x700", "0x708", "default")],
                utc_now=lambda: "2026-01-01T00:00:00Z",
                probe_worker_factory=scripted_factory([
                    probe_result(responded=True), probe_result(responded=True),
                ]),
            )
            self.assertEqual(
                [row["candidate_id"] for row in repository.discovery_candidates()],
                ["700-708-default", "700-708-extended"],
            )

    def test_probe_config_preserves_session_negative_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)

            def factory(config_path: Path):
                config = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    [row["tolerate_negative"] for row in config["session_setup"]],
                    [True, False],
                )
                result = probe_result(responded=True)
                result["candidate_id"] = config["candidate_id"]
                result["backend_kind"] = next(iter(config["device"]))
                return ScriptedProbeWorker(result)

            discover_endpoints(
                repository, root, [("0x700", "0x708", "extended")],
                utc_now=lambda: "2026-01-01T00:00:00Z",
                probe_worker_factory=factory,
            )

    def test_probe_result_must_match_requested_candidate_and_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)
            with self.assertRaisesRegex(RuntimeError, "candidate_id does not match"):
                discover_endpoints(
                    repository, root, [("0x700", "0x708", "extended")],
                    utc_now=lambda: "2026-01-01T00:00:00Z",
                    probe_worker_factory=lambda _: ScriptedProbeWorker(
                        probe_result(responded=True)
                    ),
                )

    def test_discover_endpoints_requires_at_least_one_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)
            with self.assertRaisesRegex(ValueError, "at least one candidate"):
                discover_endpoints(
                    repository, root, [], utc_now=lambda: "2026-01-01T00:00:00Z"
                )

    def test_verification_facts_survive_cache_rebuild(self) -> None:
        # Facts live beside the rebuildable cache, so rebuilding it (which an
        # endpoint confirmation or source import triggers) keeps them.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)
            factory = scripted_factory([probe_result(responded=True)])
            discover_endpoints(
                repository, root, [("0x700", "0x708", "extended")],
                utc_now=lambda: "2026-01-01T00:00:00Z", probe_worker_factory=factory,
            )
            self.assertEqual(len(repository.discovery_candidates()), 1)
            repository.rebuild_cache([], repository.paths.captures)
            self.assertEqual(len(repository.discovery_candidates()), 1)
            self.assertTrue(repository.verification_facts_path.is_file())


class PreflightTests(unittest.TestCase):
    def test_preflight_against_confirmed_endpoint_records_facts_without_editing_policy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)
            factory = scripted_factory([
                probe_result(responded=True, read_outcome="positive"),
                probe_result(responded=True, read_outcome="negative"),
            ])
            results = preflight_dids(
                repository, root, "700-708-extended", ["0x1075", "0x1090"],
                utc_now=lambda: "2026-01-01T00:00:00Z", probe_worker_factory=factory,
            )
            self.assertEqual([row["outcome"] for row in results], ["positive", "negative"])
            stored = repository.did_preflight_facts("700-708-extended")
            self.assertEqual(len(stored), 2)
            self.assertEqual(policy(), repository.policy)

    def test_preflight_canonicalizes_and_enforces_project_did_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)
            with self.assertRaisesRegex(ValueError, "outside the tracked project range"):
                preflight_dids(
                    repository, root, "700-708-extended", ["0x0000"],
                    utc_now=lambda: "2026-01-01T00:00:00Z",
                    probe_worker_factory=scripted_factory([]),
                )
            with self.assertRaisesRegex(ValueError, "invalid DID"):
                preflight_dids(
                    repository, root, "700-708-extended", ["0x100"],
                    utc_now=lambda: "2026-01-01T00:00:00Z",
                    probe_worker_factory=scripted_factory([]),
                )

    def test_positive_preflight_supplies_planning_cost_fact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)
            preflight_dids(
                repository, root, "700-708-extended", ["0x1075"],
                utc_now=lambda: "2026-01-01T00:00:00Z",
                probe_worker_factory=scripted_factory([
                    probe_result(responded=True, read_outcome="positive")
                ]),
            )
            self.assertEqual(
                repository.acquisition_cost_facts()["700-708-extended/0x1075"],
                {
                    "sample_count": 1,
                    "latency_p95_ms": 8,
                    "maximum_response_bytes": 5,
                    "outcomes": {"positive": 1},
                },
            )

    def test_preflight_requires_at_least_one_did(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)
            with self.assertRaisesRegex(ValueError, "at least one DID"):
                preflight_dids(
                    repository, root, "700-708-extended", [],
                    utc_now=lambda: "2026-01-01T00:00:00Z",
                )

    def test_preflight_targets_a_responding_discovery_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)
            discover_endpoints(
                repository, root, [("0x7D2", "0x7DA", "extended")],
                utc_now=lambda: "2026-01-01T00:00:00Z",
                probe_worker_factory=scripted_factory([probe_result(responded=True)]),
            )
            results = preflight_dids(
                repository, root, "7d2-7da-extended", ["0x1075"],
                utc_now=lambda: "2026-01-01T00:00:01Z",
                probe_worker_factory=scripted_factory(
                    [probe_result(responded=True, read_outcome="positive")]
                ),
            )
            self.assertEqual(results[0]["outcome"], "positive")

    def test_preflight_rejects_a_non_responding_discovery_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)
            discover_endpoints(
                repository, root, [("0x7D2", "0x7DA", "extended")],
                utc_now=lambda: "2026-01-01T00:00:00Z",
                probe_worker_factory=scripted_factory([probe_result(responded=False)]),
            )
            with self.assertRaisesRegex(ValueError, "did not respond"):
                preflight_dids(
                    repository, root, "7d2-7da-extended", ["0x1075"],
                    utc_now=lambda: "2026-01-01T00:00:01Z",
                )

    def test_preflight_rejects_an_unknown_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)
            with self.assertRaisesRegex(ValueError, "neither a confirmed endpoint"):
                preflight_dids(
                    repository, root, "unknown-endpoint", ["0x1075"],
                    utc_now=lambda: "2026-01-01T00:00:00Z",
                )

    def test_session_unavailable_read_is_recorded_without_a_transport_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_project(root)
            repository = make_repository(root)
            factory = scripted_factory([
                probe_result(responded=False, read_outcome="session_unavailable")
            ])
            results = preflight_dids(
                repository, root, "700-708-extended", ["0x1075"],
                utc_now=lambda: "2026-01-01T00:00:00Z", probe_worker_factory=factory,
            )
            self.assertEqual(results[0]["outcome"], "session_unavailable")


class PromotionTests(unittest.TestCase):
    def test_promote_confirmed_endpoint_builds_a_valid_transport_document(self) -> None:
        candidate = {
            "candidate_id": "7d2-7da-extended",
            "tester_can_id": "0x7D2",
            "response_can_id": "0x7DA",
            "session_recipe_id": "extended",
            "responded": True,
        }
        updated = promote_confirmed_endpoint(
            transport(), candidate, [397], ["docs/findings/example.md"]
        )
        validate_transport(updated)
        ids = [row["id"] for row in updated["endpoints"]]
        self.assertEqual(ids, sorted(ids))
        self.assertIn("7d2-7da-extended", ids)
        promoted = next(row for row in updated["endpoints"] if row["id"] == "7d2-7da-extended")
        self.assertEqual(promoted["state"], "confirmed")
        self.assertEqual(promoted["ecu_keys"], [397])

    def test_promote_confirmed_endpoint_rejects_a_non_responding_candidate(self) -> None:
        candidate = {
            "candidate_id": "7d2-7da-extended",
            "tester_can_id": "0x7D2",
            "response_can_id": "0x7DA",
            "session_recipe_id": "extended",
            "responded": False,
        }
        with self.assertRaisesRegex(ValueError, "only a responding"):
            promote_confirmed_endpoint(transport(), candidate, [397], ["docs/example.md"])

    def test_promote_confirmed_endpoint_rejects_conflicting_ecu_keys(self) -> None:
        candidate = {
            "candidate_id": "701-709-extended",
            "tester_can_id": "0x701",
            "response_can_id": "0x709",
            "session_recipe_id": "extended",
            "responded": True,
        }
        with self.assertRaisesRegex(ValueError, "already belong to a confirmed endpoint"):
            promote_confirmed_endpoint(transport(), candidate, [372], ["docs/example.md"])

    def test_promote_confirmed_endpoint_requires_provenance(self) -> None:
        candidate = {
            "candidate_id": "701-709-extended",
            "tester_can_id": "0x701",
            "response_can_id": "0x709",
            "session_recipe_id": "extended",
            "responded": True,
        }
        with self.assertRaisesRegex(ValueError, "at least one provenance"):
            promote_confirmed_endpoint(transport(), candidate, [500], [])


class ProbeClientTests(unittest.TestCase):
    def test_wall_clock_timeout_kills_the_probe_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "probe.json"
            write_json(config_path, {
                "transport": {
                    "initial_response_timeout_ms": 100,
                    "response_pending_budget_ms": 200,
                },
                "session_setup": [{}],
            })
            process = mock.Mock()
            process.communicate.side_effect = [
                subprocess.TimeoutExpired("probe", 3.3),
                ("", ""),
            ]
            with mock.patch("toyota_telemetry.discovery.subprocess.Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "wall-clock bound"):
                    JsonlProbeClient(Path("worker.exe"), config_path).probe()
            process.kill.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
