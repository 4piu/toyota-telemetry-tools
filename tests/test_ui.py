"""Offscreen tests for the four-page desktop interface over the operator service."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from tests.operator_fixture import (
    CANDIDATE_ID,
    ENDPOINT_ID,
    UNCONFIRMED_KEY,
    ScriptedProbes,
    make_operator_project,
)
from tests.test_operator_service import FakeWorker
from toyota_telemetry.operator import OPERATION_IDS, bounded_minmax
from toyota_telemetry.ui.shell import OperatorWindow


@pytest.fixture(autouse=True)
def no_modal_boxes():
    """A modal box would block the offscreen run; surface its text instead."""

    def fail(_parent, title, text, *args, **kwargs):
        raise AssertionError(f"unexpected message box {title!r}: {text}")

    with mock.patch.object(QMessageBox, "warning", side_effect=fail), \
            mock.patch.object(QMessageBox, "information", side_effect=fail), \
            mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
        yield


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vehicle"
    root.mkdir()
    rows = make_operator_project(root)
    return root, rows


@pytest.fixture
def probes():
    scripted = ScriptedProbes()
    with mock.patch("toyota_telemetry.discovery.default_probe_worker_factory", lambda repository, root: scripted):
        yield scripted


def open_window(qtbot, root: Path | None = None) -> OperatorWindow:
    window = OperatorWindow(root, restore_settings=False)
    qtbot.addWidget(window)
    window.show()
    return window


def import_rows(window: OperatorWindow, rows: Path) -> None:
    setup = window.setup_page
    setup.file_path.setText(str(rows))
    setup.preview_button.click()
    assert setup.apply_button.isEnabled(), setup.preview_result.text()
    setup.apply_button.click()


def wait_for_job(qtbot, page) -> None:
    qtbot.waitUntil(lambda: page.job is None, timeout=10_000)


def test_starts_without_a_project_or_adapter(qtbot) -> None:
    window = open_window(qtbot)
    assert window.project_path is None
    assert [window.navigation.item(row).text() for row in range(4)] == ["Setup", "Signals", "Collect", "Recordings"]
    assert window.next_text.text() == "Open an existing project"
    assert window.adapter_status.text() == "Adapter closed"
    assert not window.signals_page.verify_button.isEnabled()
    assert "Open a project" in window.signals_page.action_reason.text()
    assert not window.collect_page.start_button.isEnabled()


def test_every_operation_has_a_desktop_action_with_cli_hint(qtbot) -> None:
    window = open_window(qtbot)
    assert set(window.context.actions) == set(OPERATION_IDS)
    for operation_id, widget in window.context.actions.items():
        assert "Command line: telemetry" in widget.toolTip(), operation_id


def test_setup_imports_sources_and_saves_connector_without_opening_it(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    assert window.project_path == root.resolve()
    assert "source import" in window.next_command.text()
    import_rows(window, rows)
    assert "Using candidates.json" in window.setup_page.preview_result.text()
    assert json.loads((root / "result/catalog-manifest.json").read_text())["catalog_row_count"] == 4
    setup = window.setup_page
    setup.connector_kind.setCurrentIndex(setup.connector_kind.findData("slcan"))
    setup.connector_fields["slcan"]["port"].setText("COM7")
    setup.executable.setText("")
    setup.connector_save_button.click()
    saved = json.loads((root / "scratch/local-settings.json").read_text())
    assert saved["backend"] == {
        "kind": "slcan", "executable": "scratch/bin/telemetry-slcan.exe", "settings": {"port": "COM7"},
    }
    assert window.adapter_status.text() == "Adapter closed"


def test_signals_tree_verifies_checked_rows_after_confirmation(qtbot, project, probes) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(window, rows)
    page = window.signals_page
    assert page.tree.topLevelItemCount() == 2
    page.check("did", f"{UNCONFIRMED_KEY}/0x2001")
    assert page.verify_button.isEnabled()
    assert page.verify_button.text().startswith("LIVE")

    page.confirm_live = lambda dialog: False
    page.verify_button.click()
    assert probes.transmissions == 0

    shown = []

    def accept(dialog):
        shown.append(dialog)
        assert dialog.cancel_button.isDefault()
        return True

    page.confirm_live = accept
    page.verify_button.click()
    wait_for_job(qtbot, page)
    assert probes.transmissions == 2
    assert "1 endpoint(s) (1 to probe) and 1 DID(s)" in shown[0].findChild(type(page.action_reason)).text()
    assert "Finished: 1 positive" in page.job_log.toPlainText()
    endpoint = page.tree_data.endpoint(UNCONFIRMED_KEY)
    assert endpoint.state == "responded"
    assert "Confirm responding endpoint" in window.next_text.text()

    page.select_row("endpoint", UNCONFIRMED_KEY)
    assert page.confirm_button.isEnabled()

    def fill_confirm(dialog):
        dialog.source.setText("docs/evidence/probe.md")
        return True

    page.run_dialog = fill_confirm
    page.confirm_button.click()
    transport = json.loads((root / "config/transport.json").read_text())
    assert CANDIDATE_ID in [row["id"] for row in transport["endpoints"]]
    assert page.tree_data.endpoint(CANDIDATE_ID).confirmed


def test_target_batch_edit_is_a_draft_until_saved(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(window, rows)
    page = window.signals_page
    policy_path = root / "config/collection-policy.json"
    before = policy_path.read_bytes()
    page.check("signal", "372/0x1003/0..7")
    page.check("signal", "372/0x1003/8..15")

    seen = {}

    def edit(dialog):
        dialog.set_field("minimum_samples", 25)
        seen["impact"] = dialog.impact.text()
        return True

    page.run_dialog = edit
    page.target_button.click()
    assert "2 signal(s) across 1 DID(s)" in seen["impact"]
    assert page.dirty
    assert window.draft_status.text() == "Unsaved changes"
    assert policy_path.read_bytes() == before
    assert page.tree_data.find_signal("372/0x1003/0..7")[2].target["requirements"] == {"minimum_samples": 25}
    page.save_button.click()
    saved = json.loads(policy_path.read_text())
    assert [row["signal_ref"] for row in saved["targets"]] == ["372/0x1003/0..7", "372/0x1003/8..15"]
    assert not page.dirty


def test_external_edit_blocks_save_of_a_stale_draft(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(window, rows)
    page = window.signals_page
    page.check("signal", "372/0x1003/0..7")
    page.run_dialog = lambda dialog: (dialog.set_field("minimum_samples", 5), True)[1]
    page.target_button.click()
    policy_path = root / "config/collection-policy.json"
    document = json.loads(policy_path.read_text())
    document["storage"]["segment_seconds"] = 30
    policy_path.write_text(json.dumps(document, indent=2) + "\n")
    assert page.save() is False
    assert not page.external_notice.isHidden()
    assert json.loads(policy_path.read_text())["targets"] == []
    page.discard_button.click()
    assert not page.dirty


def test_closing_with_a_draft_offers_save_discard_cancel(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(window, rows)
    page = window.signals_page
    page.check("signal", "372/0x1003/0..7")
    page.run_dialog = lambda dialog: (dialog.set_field("minimum_samples", 5), True)[1]
    page.target_button.click()
    with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
        assert not window.close()
    with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
        assert window.close()


def test_collect_page_explains_problems_then_runs_the_exact_scope(qtbot, project, probes) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(window, rows)
    collect = window.collect_page
    window.navigate("collect")
    assert not collect.start_button.isEnabled()
    assert "Start is available once readiness" in collect.start_reason.text()
    assert "no included targets" in collect.readiness.problems[0].message
    signals = window.signals_page
    signals.check("signal", "372/0x1002/0..15")
    signals.run_dialog = lambda dialog: (dialog.set_field("minimum_samples", 2), True)[1]
    signals.target_button.click()
    signals.save_button.click()
    collect.check_readiness()
    assert collect.readiness.ready, collect.readiness.problems
    assert collect.start_button.isEnabled()
    collect.confirm_live = lambda dialog: True
    with mock.patch("toyota_telemetry.collection_service.JsonlWorkerClient", FakeWorker):
        collect.start_button.click()
        assert collect.start_button.text() == "Stop safely"
        wait_for_job(qtbot, collect)
    assert "Completed" in collect.run_state.text()
    assert window.adapter_status.text() == "Adapter closed"
    assert len(window.collect_page.context.service.runs()) == 2


def test_recordings_lists_runs_plots_bounded_series_and_exports(qtbot, project, tmp_path) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(window, rows)
    page = window.recordings_page
    window.navigate("recordings")
    assert page.runs.rowCount() == 1
    assert not page.recovery.isVisible()
    page.runs.selectRow(0)
    assert "Segment 000001 complete: 1 reads" in page.run_detail.toPlainText()
    page.select_signal("372/0x1002/0..15")
    assert page.plot.source_points == 1
    assert "Constant value" in page.plot_summary.text()
    target = tmp_path / "out.csv"
    page.choose_export_path = lambda: str(target)
    page.export_button.click()
    assert target.read_text().count("\n") == 2


def test_bounded_reduction_keeps_plots_small() -> None:
    xs = list(range(1_000_000))
    series = bounded_minmax(xs, [float(value % 97) for value in xs], 1000)
    assert series.source_points == 1_000_000
    assert series.rendered_points <= 4000
