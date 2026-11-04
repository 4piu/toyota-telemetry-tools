from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import uuid

from tests.operator_fixture import (
    CANDIDATE_ID,
    ENDPOINT_ID,
    UNCONFIRMED_KEY,
    ScriptedProbes,
    make_operator_project,
)
from toyota_telemetry.capture_writer import NativeRunWriter
from toyota_telemetry.operator import OPERATIONS, OperatorService
from toyota_telemetry.operator.editing import format_condition, parse_condition, parse_duration
from toyota_telemetry.operator.inventory import (
    ENDPOINT_NOT_VERIFIED,
    FAILED,
    NOT_VERIFIED,
    VERIFIED,
    load_inventory,
)
from toyota_telemetry.operator.live import (
    ConfirmationRefused,
    LiveBusy,
    LiveLock,
    record_preview,
    require_previewed,
)
from toyota_telemetry.operator.sources import NormalizedFileSource


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name) / "vehicle"
        self.root.mkdir()
        self.rows = make_operator_project(self.root)
        self.service = OperatorService(self.root)
        self.probes = ScriptedProbes()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def import_rows(self) -> None:
        preview = self.service.preview_source(NormalizedFileSource(self.rows).read())
        self.service.apply_source(preview)

    def verify(self, *tokens: str, **filters) -> dict:
        tree = self.service.tree()
        selection = self.service.select(tree, tokens, **filters)
        scope = self.service.verification_scope(tree, selection)
        return self.service.verify(scope, probe_worker_factory=self.probes).as_dict()

    def confirm_candidate(self) -> None:
        self.verify(UNCONFIRMED_KEY)
        self.service.confirm_endpoint(CANDIDATE_ID, [405], ["docs/evidence/probe.md"])


class SourceImportTests(ServiceTestCase):
    def test_preview_reports_counts_and_rejections_without_writing(self) -> None:
        manifest_before = (self.root / "config/catalog-manifest.json").read_bytes()
        preview = self.service.preview_source(NormalizedFileSource(self.rows).read())
        self.assertEqual(preview.counts["signals"], 4)
        self.assertEqual(preview.counts["endpoint_candidates"], 1)
        self.assertEqual(preview.counts["added"], 4)
        reasons = dict(preview.rejections)
        self.assertIn("outside the transport safety range", reasons["405/0xF100/0..7"])
        self.assertIn("needs signal_ref", reasons["row 5"])
        self.assertIsNone(preview.blocking_problem)
        self.assertFalse((self.root / "state/inventory.json").exists())
        self.assertEqual((self.root / "config/catalog-manifest.json").read_bytes(), manifest_before)

    def test_apply_keeps_unconfirmed_rows_out_of_the_cache_and_paths_out_of_provenance(self) -> None:
        self.import_rows()
        inventory = load_inventory(self.service.repository())
        self.assertEqual(len(inventory["rows"]), 4)
        variants = self.service.repository().catalog_variants()
        self.assertEqual({row["signal_ref"] for row in variants},
                         {"372/0x1002/0..15", "372/0x1003/0..7", "372/0x1003/8..15"})
        manifest = json.loads((self.root / "config/catalog-manifest.json").read_text())
        self.assertEqual(manifest["catalog_row_count"], 4)
        self.assertEqual(manifest["sources"], [{"kind": "file", "label": "candidates.json", "row_count": 4}])
        self.assertNotIn(str(self.root), json.dumps(manifest))
        second = self.service.preview_source(NormalizedFileSource(self.rows).read())
        self.assertEqual((second.counts["added"], second.counts["unchanged"]), (0, 4))

    def test_reimport_that_drops_a_reviewed_signal_is_blocked(self) -> None:
        self.import_rows()
        reduced = self.root / "state/candidates.json"
        rows = json.loads(reduced.read_text())
        reduced.write_text(json.dumps([row for row in rows if row.get("name") != "Battery voltage"]))
        preview = self.service.preview_source(NormalizedFileSource(reduced).read())
        self.assertIn("reviewed signals would no longer resolve", preview.blocking_problem)
        with self.assertRaisesRegex(ValueError, "no longer resolve"):
            self.service.apply_source(preview)


class TreeAndSelectionTests(ServiceTestCase):
    def test_tree_groups_signals_and_reports_verification_state(self) -> None:
        self.import_rows()
        tree = self.service.tree()
        confirmed = tree.endpoint(ENDPOINT_ID)
        candidate = tree.endpoint(UNCONFIRMED_KEY)
        self.assertTrue(confirmed.confirmed)
        self.assertEqual([did.data_id for did in confirmed.dids], ["0x1002", "0x1003"])
        self.assertEqual(confirmed.dids[0].state, VERIFIED)
        self.assertEqual(confirmed.dids[1].state, NOT_VERIFIED)
        self.assertEqual(len(confirmed.dids[1].signals), 2)
        self.assertEqual(candidate.state, "not verified")
        self.assertEqual(candidate.ecu_names, ["Brake"])
        self.assertEqual(candidate.dids[0].state, ENDPOINT_NOT_VERIFIED)

    def test_selectors_and_filters_resolve_finite_sets(self) -> None:
        self.import_rows()
        tree = self.service.tree()
        select = self.service.select
        self.assertEqual(select(tree, [ENDPOINT_ID]).dids, [f"{ENDPOINT_ID}/0x1002", f"{ENDPOINT_ID}/0x1003"])
        self.assertEqual(select(tree, [f"{ENDPOINT_ID}/0x1003/8..15"]).signals, ["372/0x1003/8..15"])
        self.assertEqual(select(tree, ["372/0x1003/0..7"]).signals, ["372/0x1003/0..7"])
        self.assertEqual(select(tree, states=["not-verified"]).dids,
                         [f"{ENDPOINT_ID}/0x1003", f"{UNCONFIRMED_KEY}/0x2001"])
        self.assertEqual(select(tree, search="coolant").signals, ["372/0x1003/8..15"])
        self.assertEqual(select(tree, ecus=[405]).dids, [f"{UNCONFIRMED_KEY}/0x2001"])
        manual = select(tree, [f"{ENDPOINT_ID}/0x1FFF"])
        self.assertEqual(manual.manual_dids, [f"{ENDPOINT_ID}/0x1FFF"])
        with self.assertRaisesRegex(ValueError, "nothing selected"):
            select(tree, [])
        with self.assertRaisesRegex(ValueError, "unknown endpoint"):
            select(tree, ["7ff-7f7"])
        with self.assertRaisesRegex(ValueError, "unknown state filter"):
            select(tree, states=["green"])


class VerificationTests(ServiceTestCase):
    def test_unverified_endpoint_is_probed_before_its_dids(self) -> None:
        self.import_rows()
        result = self.verify(UNCONFIRMED_KEY)
        self.assertEqual([config.get("preflight_read") is None for config in self.probes.configs], [True, False])
        self.assertEqual(result["endpoints"][0]["target_id"], CANDIDATE_ID)
        self.assertEqual(result["outcomes"], {"positive": 1})
        tree = self.service.tree()
        self.assertEqual(tree.endpoint(UNCONFIRMED_KEY).state, "responded")
        self.assertEqual(tree.endpoint(UNCONFIRMED_KEY).dids[0].state, VERIFIED)

    def test_silent_endpoint_skips_its_dids(self) -> None:
        self.import_rows()
        self.probes.endpoint_responds = False
        result = self.verify(UNCONFIRMED_KEY)
        self.assertEqual(self.probes.transmissions, 1)
        self.assertEqual(result["dids"][0]["outcome"], "skipped")
        self.assertEqual(self.service.tree().endpoint(UNCONFIRMED_KEY).state, "silent")

    def test_negative_did_is_failed_and_facts_survive_confirmation_rebuild(self) -> None:
        self.import_rows()
        self.probes.did_outcome = "negative"
        self.verify(f"{ENDPOINT_ID}/0x1003")
        self.assertEqual(self.service.tree().find_did(f"{ENDPOINT_ID}/0x1003")[1].state, FAILED)
        self.probes.did_outcome = "positive"
        self.confirm_candidate()
        tree = self.service.tree()
        self.assertTrue(tree.endpoint(CANDIDATE_ID).confirmed)
        self.assertEqual(tree.find_did(f"{CANDIDATE_ID}/0x2001")[1].state, VERIFIED)
        self.assertEqual(tree.find_did(f"{ENDPOINT_ID}/0x1003")[1].state, FAILED)
        transport = json.loads((self.root / "config/transport.json").read_text())
        self.assertIn(CANDIDATE_ID, [row["id"] for row in transport["endpoints"]])

    def test_scope_is_bounded_hashed_and_rejects_out_of_range_dids(self) -> None:
        self.import_rows()
        tree = self.service.tree()
        scope = self.service.verification_scope(tree, self.service.select(tree, [UNCONFIRMED_KEY]))
        self.assertEqual((scope.probe_count, scope.did_count, scope.endpoint_probe_count), (2, 1, 1))
        self.assertGreater(scope.upper_bound_seconds, 0)
        again = self.service.verification_scope(tree, self.service.select(tree, [UNCONFIRMED_KEY]))
        self.assertEqual(scope.digest, again.digest)
        with self.assertRaisesRegex(ValueError, "safety range"):
            self.service.verification_scope(tree, self.service.select(tree, [f"{ENDPOINT_ID}/0xFFFF"]))

    def test_confirmation_requires_a_responding_verified_candidate(self) -> None:
        self.import_rows()
        with self.assertRaisesRegex(ValueError, "no verification result"):
            self.service.confirm_endpoint(CANDIDATE_ID, [405], ["docs/evidence/probe.md"])


class EditingTests(ServiceTestCase):
    def test_target_set_creates_shared_settings_and_requires_paired_difference(self) -> None:
        self.import_rows()
        tree = self.service.tree()
        result = self.service.edit_targets(
            tree, ["372/0x1003/8..15"], requirements={"minimum_samples": 10, "minimum_span_seconds": 600.0}
        )
        self.assertEqual(result.created_settings, [f"{ENDPOINT_ID}/0x1003"])
        self.service.save(policy=result.policy)
        policy = self.service.repository().policy
        self.assertEqual(policy["targets"][0]["requirements"],
                         {"minimum_samples": 10, "minimum_span_seconds": 600.0})
        with self.assertRaisesRegex(ValueError, "needs a minimum difference"):
            self.service.edit_targets(self.service.tree(), ["372/0x1003/8..15"],
                                      requirements={"minimum_changes": 3})
        removed = self.service.edit_targets(self.service.tree(), ["372/0x1003/8..15"],
                                            requirements={"minimum_span_seconds": 0})
        self.assertEqual(removed.policy["targets"][0]["requirements"], {"minimum_samples": 10})

    def test_targets_and_reviews_need_a_confirmed_endpoint(self) -> None:
        self.import_rows()
        tree = self.service.tree()
        with self.assertRaisesRegex(ValueError, "needs a confirmed endpoint"):
            self.service.edit_targets(tree, ["405/0x2001/0..7"], requirements={"minimum_samples": 1})
        with self.assertRaisesRegex(ValueError, "needs a confirmed endpoint"):
            self.service.edit_signals(tree, ["405/0x2001/0..7"], name="Brake")

    def test_did_settings_report_unselected_siblings(self) -> None:
        self.import_rows()
        tree = self.service.tree()
        result = self.service.edit_dids(
            tree, [f"{ENDPOINT_ID}/0x1003"], selected_signals=["372/0x1003/0..7"],
            target_interval_ms=200, maximum_interval_ms=400,
        )
        self.assertEqual(result.siblings, ["372/0x1003/8..15"])
        self.assertEqual(result.created_settings, [f"{ENDPOINT_ID}/0x1003"])
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            self.service.edit_dids(tree, [f"{ENDPOINT_ID}/0x1003"], target_interval_ms=900, maximum_interval_ms=400)

    def test_signal_review_and_clear_target(self) -> None:
        self.import_rows()
        tree = self.service.tree()
        review = self.service.edit_signals(tree, ["372/0x1003/0..7"], name="Shift position", evidence_state="Reviewed")
        self.service.save(review=review.review)
        self.assertEqual(self.service.tree().find_signal("372/0x1003/0..7")[2].name, "Shift position")
        with self.assertRaisesRegex(ValueError, "invalid formula"):
            self.service.edit_signals(tree, ["372/0x1003/0..7"], formula="import os")
        target = self.service.edit_targets(tree, ["372/0x1003/0..7"], requirements={"required_states": [1, 2]})
        self.service.save(policy=target.policy)
        cleared = self.service.clear_targets(["372/0x1003/0..7"])
        self.assertEqual(cleared.policy["targets"], [])

    def test_condition_and_duration_text(self) -> None:
        node = parse_condition("372/0x1002/0..15 >= 10 and 372/0x1003/0..7 in 1,2", 1500)
        self.assertEqual(node["all"][1]["value"], [1, 2])
        self.assertEqual(node["all"][0]["maximum_age_ms"], 1500)
        self.assertEqual(format_condition(node), "372/0x1002/0..15 >= 10 and 372/0x1003/0..7 in 1,2")
        self.assertIsNone(parse_condition("none"))
        with self.assertRaisesRegex(ValueError, "not both"):
            parse_condition("372/0x1002/0..15 > 1 and 372/0x1002/0..15 < 9 or 372/0x1003/0..7 == 1")
        with self.assertRaisesRegex(ValueError, "expected SIGNAL OP VALUE"):
            parse_condition("speed is fast")
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("90"), 90)


class ReadinessAndCollectionTests(ServiceTestCase):
    def target_battery(self, samples: int = 3) -> None:
        tree = self.service.tree()
        result = self.service.edit_targets(tree, ["372/0x1002/0..15"], requirements={"minimum_samples": samples})
        self.service.save(policy=result.policy)

    def test_readiness_reports_fixes_then_becomes_ready(self) -> None:
        self.import_rows()
        readiness = self.service.readiness()
        self.assertFalse(readiness.ready)
        self.assertIn("no included targets", readiness.problems[0].message)
        self.assertIn("telemetry target set", readiness.problems[0].command)
        self.assertEqual(readiness.problems[0].hint, "Set collection targets")
        tree = self.service.tree()
        result = self.service.edit_targets(tree, ["372/0x1003/0..7"], requirements={"minimum_samples": 1})
        self.service.save(policy=result.policy)
        readiness = self.service.readiness()
        self.assertEqual(readiness.problems[0].command, f"telemetry verify {ENDPOINT_ID}/0x1003")
        self.verify(f"{ENDPOINT_ID}/0x1003")
        readiness = self.service.readiness()
        self.assertTrue(readiness.ready, readiness.problems)
        self.assertEqual(readiness.endpoint_id, ENDPOINT_ID)
        self.assertEqual(readiness.scope["targets"], ["372/0x1003/0..7"])
        self.assertEqual(readiness.digest, self.service.readiness().digest)

    def test_collect_runs_the_ready_scope_and_updates_progress(self) -> None:
        self.import_rows()
        self.target_battery(samples=3)
        readiness = self.service.readiness()
        self.assertTrue(readiness.ready, readiness.problems)
        events: list[dict] = []
        from unittest import mock
        with mock.patch("toyota_telemetry.collection_service.JsonlWorkerClient", FakeWorker):
            reason = self.service.collect(readiness, note="bench", event_sink=events.append,
                                          stop_event=threading.Event())
        self.assertEqual(reason, "plan_complete")
        self.assertEqual(events[0]["event"], "scope")
        runs = self.service.runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[-1].note, "bench")
        self.assertTrue(self.service.tree().find_signal("372/0x1002/0..15")[2].progress["complete"])
        self.assertEqual(self.service.status().next_action.page, "recordings")

    def test_live_lock_is_exclusive_and_scripted_runs_need_a_matching_preview(self) -> None:
        repository = self.service.repository()
        with LiveLock(repository):
            with self.assertRaises(LiveBusy):
                with LiveLock(repository):
                    pass
            self.assertTrue(self.service.status().live_busy)
        with self.assertRaisesRegex(ConfirmationRefused, "no previewed"):
            require_previewed(repository, "collect", "a" * 64)
        record_preview(repository, "collect", "a" * 64)
        require_previewed(repository, "collect", "a" * 64)
        with self.assertRaisesRegex(ConfirmationRefused, "changed since"):
            require_previewed(repository, "collect", "b" * 64)


class RecordingsTests(ServiceTestCase):
    def test_interrupted_run_is_listed_recovered_or_quarantined(self) -> None:
        self.import_rows()
        self.target_and_crash()
        runs = {run.run_id: run for run in self.service.runs()}
        interrupted = [run for run in runs.values() if run.state == "needs recovery"]
        self.assertEqual(len(interrupted), 1)
        status = self.service.status()
        self.assertEqual(status.next_action.command, f"telemetry recordings recover {interrupted[0].run_id}")
        self.service.recover(interrupted[0].run_id)
        self.assertEqual(self.service.run(interrupted[0].run_id).end_reason, "recovered_prefix")

    def test_trash_moves_an_unrecoverable_run_to_the_system_trash(self) -> None:
        from toyota_telemetry import system_trash

        self.import_rows()
        run_id = self.target_and_crash(exchanges=0)
        with self.assertRaisesRegex(ValueError, "no recoverable exchange"):
            self.service.recover(run_id)
        bin_ = Path(self._directory.name) / "fake-bin"
        bin_.mkdir()

        def fake(paths):
            for path in paths:
                path.rename(bin_ / path.name)

        with mock.patch.object(system_trash, "send_to_trash", fake):
            drive = self.service.trash(run_id)
        self.assertIn(run_id, drive.run_ids)
        self.assertNotIn(run_id, [run.run_id for run in self.service.runs()])
        self.assertEqual(self.service.observation_summary(drive.run_ids), {})
        # Restoring from the system trash puts the folder back; the drive returns.
        (bin_ / run_id).rename(self.service.repository().paths.captures / run_id)
        self.assertIn(run_id, [run.run_id for run in self.service.runs()])

        def refuse(_paths):
            raise system_trash.TrashError("no Recycle Bin")

        with mock.patch.object(system_trash, "send_to_trash", refuse):
            with self.assertRaisesRegex(ValueError, "not moved to the trash: no Recycle Bin"):
                self.service.trash(run_id)

    def test_freedesktop_trash_records_where_a_folder_came_from(self) -> None:
        from toyota_telemetry import system_trash

        base = Path(self._directory.name)
        folder = base / "some run"
        folder.mkdir()
        (folder / "data.txt").write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(base / "data")}):
            system_trash._freedesktop([folder.resolve()])
        trash = base / "data" / "Trash"
        self.assertFalse(folder.exists())
        self.assertTrue((trash / "files" / "some run" / "data.txt").exists())
        info = (trash / "info" / "some run.trashinfo").read_text(encoding="utf-8")
        self.assertIn("Path=", info)
        self.assertIn("some%20run", info)

    def test_runs_group_into_drives_by_pause(self) -> None:
        from toyota_telemetry.operator.recordings import RunSummary, group_drives

        def run(run_id: str, start: str, seconds: float) -> RunSummary:
            return RunSummary(run_id, "complete", created_utc=start, duration_seconds=seconds)

        drives = group_drives([
            run("c", "2026-09-01T08:20:30Z", 1),
            run("a", "2026-09-01T08:15:00Z", 300),   # ends 08:20:00
            run("b", "2026-09-01T08:20:59Z", 1),     # 59 s after a's end: same drive
            run("d", "2026-09-01T08:22:01Z", 5),     # 61 s after b's end: new drive
            run("e", None, 0),
        ])
        self.assertEqual([[r.run_id for r in drive.runs] for drive in drives], [["a", "c", "b"], ["d"], ["e"]])
        self.assertEqual(drives[0].key, "2026-09-01T08:15")
        self.assertEqual(drives[0].duration_seconds, 360)

    def test_series_and_export_follow_run_start_time(self) -> None:
        self.import_rows()
        tree = self.service.tree()
        result = self.service.edit_targets(tree, ["372/0x1002/0..15"], requirements={"minimum_samples": 9})
        self.service.save(policy=result.policy)
        self.service.readiness()
        plan_path = self.root / "state/plans/admitted-plan.json"
        plan = json.loads(plan_path.read_text())
        acquisition = plan["acquisitions"][0]
        run_ids = []
        for created in ("2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z"):
            run_id = str(uuid.uuid4())
            run_ids.append(run_id)
            writer = NativeRunWriter(
                self.root / "captures", plan, plan_path.read_bytes(), run_id=run_id,
                created_utc=created, backend_kind="synthetic", disk_free_bytes=lambda _: 10**12,
            )
            writer.append_exchange({
                "acquisition_id": acquisition["acquisition_id"], "endpoint_id": acquisition["endpoint_id"],
                "data_id": acquisition["data_id"], "response_utc": created, "request_elapsed_ms": 0,
                "response_elapsed_ms": 5, "request_hex": acquisition["request_hex"],
                "response_hex": "62 10 02 00 01", "outcome": "positive", "negative_response_code": None,
                "pending_count": 0, "pending_duration_ms": 0, "latency_ms": 5, "retry_count": 0,
                "scheduler_reason": "target_due", "objective_ids": acquisition["objective_ids"],
                "observations": [{"signal_ref": "372/0x1002/0..15", "raw_hex": "00 01",
                                  "decoded_value": 0.001, "applicability": "true"}],
            })
            writer.finish("operator_stop")
        order = [row["run_id"] for row in self.service.series("372/0x1002/0..15")]
        self.assertEqual(order[:2], [run_ids[1], run_ids[0]])

    def test_export_writes_every_observation_as_csv(self) -> None:
        self.import_rows()
        stream = io.StringIO()
        count = self.service.export(["372/0x1002/0..15"], stream)
        self.assertEqual(count, 1)
        header, row = stream.getvalue().splitlines()
        self.assertTrue(header.startswith("signal_ref,run_id"))
        self.assertIn("34 12", row)

    def target_and_crash(self, exchanges: int = 1) -> str:
        tree = self.service.tree()
        result = self.service.edit_targets(tree, ["372/0x1002/0..15"], requirements={"minimum_samples": 9})
        self.service.save(policy=result.policy)
        readiness = self.service.readiness()
        plan_path = self.root / "state/plans/admitted-plan.json"
        plan = json.loads(plan_path.read_text())
        run_id = str(uuid.uuid4())
        writer = NativeRunWriter(
            self.root / "captures", plan, plan_path.read_bytes(), run_id=run_id,
            created_utc="2026-01-01T00:00:00Z", backend_kind="synthetic",
            disk_free_bytes=lambda _: 10**12,
        )
        acquisition = plan["acquisitions"][0]
        for index in range(exchanges):
            writer.append_exchange({
                "acquisition_id": acquisition["acquisition_id"],
                "endpoint_id": acquisition["endpoint_id"],
                "data_id": acquisition["data_id"],
                "response_utc": "2026-01-01T00:00:00Z",
                "request_elapsed_ms": index * 10,
                "response_elapsed_ms": index * 10 + 5,
                "request_hex": acquisition["request_hex"],
                "response_hex": "62 10 02 00 01",
                "outcome": "positive",
                "negative_response_code": None,
                "pending_count": 0,
                "pending_duration_ms": 0,
                "latency_ms": 5,
                "retry_count": 0,
                "scheduler_reason": "target_due",
                "objective_ids": acquisition["objective_ids"],
                "observations": [{
                    "signal_ref": "372/0x1002/0..15", "raw_hex": "00 01",
                    "decoded_value": 0.001, "applicability": "true",
                }],
            })
        writer.crash()
        self.assertTrue(readiness.ready)
        return run_id


class StatusTests(ServiceTestCase):
    def test_next_action_follows_the_workflow(self) -> None:
        self.assertEqual(self.service.status().next_action.page, "setup")
        self.assertIn("source import", self.service.status().next_action.command)
        self.import_rows()
        self.assertIn("telemetry target set", self.service.status().next_action.command)
        self.verify(UNCONFIRMED_KEY)
        self.assertIn(f"endpoint confirm {CANDIDATE_ID} --ecu 405", self.service.status().next_action.command)

    def test_invalid_project_reports_check(self) -> None:
        (self.root / "config/transport.json").write_text("{}\n")
        status = self.service.status()
        self.assertFalse(status.valid)
        self.assertEqual(status.next_action.command, "telemetry check")
        self.assertFalse(self.service.check().ok)

    def test_operations_have_unique_ids_and_both_surfaces(self) -> None:
        ids = [operation.id for operation in OPERATIONS]
        self.assertEqual(len(ids), len(set(ids)))
        for operation in OPERATIONS:
            self.assertTrue(operation.cli.startswith("telemetry "))
            self.assertTrue(operation.gui)
        self.assertEqual({op.id for op in OPERATIONS if op.live}, {"verify", "collect"})


class FakeWorker:
    """Collection worker double answering every read with a fixed value."""

    def __init__(self, executable: Path, config_path: Path) -> None:
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))

    def start(self) -> dict:
        return {"event": "ready", "schema_version": 1,
                "endpoint_id": self.config["endpoint_id"], "backend_kind": "synthetic"}

    def exchange(self, sequence: int, transaction_id: str, event_sink) -> dict:
        did = transaction_id.rsplit("/0x", 1)[1]
        return {
            "event": "result", "schema_version": 1, "sequence": sequence, "outcome": "positive",
            "response_hex": f"62 {did[:2]} {did[2:]} 00 07", "nrc": None, "pending_count": 0,
            "pending_duration_ms": 0, "latency_ms": 5, "detail": None,
        }

    def request_stop(self) -> None:
        pass

    def close(self) -> None:
        pass

    def terminate(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
