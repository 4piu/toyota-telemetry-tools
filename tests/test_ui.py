"""Offscreen tests for the four-page desktop interface over the operator service."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractButton, QLabel, QMessageBox, QWidget

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


def settle(qtbot, window: OperatorWindow) -> None:
    """Wait until background project work has finished and been shown."""

    qtbot.waitUntil(lambda: window.context.worker.idle, timeout=10_000)


def open_window(qtbot, root: Path | None = None) -> OperatorWindow:
    window = OperatorWindow(root, restore_settings=False)
    qtbot.addWidget(window)
    window.show()
    settle(qtbot, window)
    return window


def import_rows(qtbot, window: OperatorWindow, rows: Path) -> None:
    setup = window.setup_page
    setup.file_path.setText(str(rows))
    setup.preview_button.click()
    settle(qtbot, window)
    assert setup.apply_button.isEnabled(), setup.preview_result.text()
    setup.apply_button.click()
    settle(qtbot, window)


def wait_for_job(qtbot, page) -> None:
    qtbot.waitUntil(lambda: page.job is None, timeout=10_000)


def test_starts_without_a_project_or_adapter(qtbot) -> None:
    window = open_window(qtbot)
    assert window.project_path is None
    assert [window.navigation.item(row).text() for row in range(4)] == ["Setup", "Signals", "Collect", "Recordings"]
    assert window.next_text.text() == "Open a project"
    assert window.adapter_status.text() == "Adapter closed"
    assert not window.signals_page.verify_button.isEnabled()
    assert "Open a project first." in window.signals_page.verify_button.toolTip()
    assert not window.collect_page.start_button.isEnabled()


def _visible_text(widget: QWidget) -> list[str]:
    texts = [widget.toolTip(), widget.accessibleDescription()]
    if isinstance(widget, (QLabel, QAbstractButton)):
        texts.append(widget.text())
    return [text for text in texts if text]


def test_every_operation_has_a_desktop_action(qtbot, project) -> None:
    window = open_window(qtbot, project[0])
    assert set(window.context.actions) == set(OPERATION_IDS)
    for operation_id, widget in window.context.actions.items():
        assert widget.toolTip(), operation_id


def test_gui_shows_no_command_line_text_and_short_buttons(qtbot, project, probes) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    window.collect_page.check_readiness()
    settle(qtbot, window)
    for widget in window.findChildren(QWidget):
        for text in _visible_text(widget):
            assert "telemetry " not in text and " --" not in text, (widget.objectName(), text)
        if isinstance(widget, QAbstractButton) and widget.text():
            assert len(widget.text().split()) <= 2, widget.text()
            assert not widget.text().endswith(("…", "...")), widget.text()
    page = window.signals_page
    assert page.verify_button.sizeHint().height() == page.add_endpoint_button.sizeHint().height()


def test_setup_imports_sources_and_saves_connector_without_opening_it(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    assert window.project_path == root.resolve()
    assert window.next_text.text() == "Next: Import a candidate signal list"
    import_rows(qtbot, window, rows)
    assert window.setup_page.preview_result.text() == "Applied candidates.json."
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
    import_rows(qtbot, window, rows)
    page = window.signals_page
    assert page.tree.topLevelItemCount() == 2
    page.check("did", f"{UNCONFIRMED_KEY}/0x2001")
    assert page.verify_button.isEnabled()
    assert page.verify_button.text() == "Verify"
    assert page.verify_button.property("liveAction")

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
    settle(qtbot, window)
    assert probes.transmissions == 2
    assert "1 endpoint(s) (1 to probe) and 1 DID(s)" in shown[0].findChild(QLabel).text()
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
    settle(qtbot, window)
    transport = json.loads((root / "config/transport.json").read_text())
    assert CANDIDATE_ID in [row["id"] for row in transport["endpoints"]]
    assert page.tree_data.endpoint(CANDIDATE_ID).confirmed


def test_target_batch_edit_is_a_draft_until_saved(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
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
    settle(qtbot, window)
    assert "2 signal(s) on 1 DID(s) will change" in seen["impact"]
    assert page.dirty
    assert window.draft_status.text() == "Unsaved changes"
    assert policy_path.read_bytes() == before
    assert page.tree_data.find_signal("372/0x1003/0..7")[2].target["requirements"] == {"minimum_samples": 25}
    page.save_button.click()
    settle(qtbot, window)
    saved = json.loads(policy_path.read_text())
    assert [row["signal_ref"] for row in saved["targets"]] == ["372/0x1003/0..7", "372/0x1003/8..15"]
    assert not page.dirty


def test_external_edit_blocks_save_of_a_stale_draft(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    page = window.signals_page
    page.check("signal", "372/0x1003/0..7")
    page.run_dialog = lambda dialog: (dialog.set_field("minimum_samples", 5), True)[1]
    page.target_button.click()
    settle(qtbot, window)
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
    import_rows(qtbot, window, rows)
    page = window.signals_page
    page.check("signal", "372/0x1003/0..7")
    page.run_dialog = lambda dialog: (dialog.set_field("minimum_samples", 5), True)[1]
    page.target_button.click()
    settle(qtbot, window)
    with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
        assert not window.close()
    with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
        assert window.close()


def test_collect_page_explains_problems_then_runs_the_exact_scope(qtbot, project, probes) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    collect = window.collect_page
    window.navigate("collect")
    settle(qtbot, window)
    assert not collect.start_button.isEnabled()
    assert "Readiness has problems." in collect.start_button.toolTip()
    assert collect.problem_area.itemAt(0).widget().findChild(QLabel).text() == "Set collection targets"
    assert "no included targets" in collect.readiness.problems[0].message
    signals = window.signals_page
    signals.check("signal", "372/0x1002/0..15")
    signals.run_dialog = lambda dialog: (dialog.set_field("minimum_samples", 2), True)[1]
    signals.target_button.click()
    settle(qtbot, window)
    signals.save_button.click()
    settle(qtbot, window)
    collect.check_readiness()
    settle(qtbot, window)
    assert collect.readiness.ready, collect.readiness.problems
    assert collect.start_button.isEnabled()
    collect.confirm_live = lambda dialog: True
    with mock.patch("toyota_telemetry.collection_service.JsonlWorkerClient", FakeWorker):
        collect.start_button.click()
        assert collect.start_button.text() == "Stop"
        wait_for_job(qtbot, collect)
    settle(qtbot, window)
    assert "Completed" in collect.run_state.text()
    assert window.adapter_status.text() == "Adapter closed"
    assert len(window.collect_page.context.service.runs()) == 2


def test_recordings_lists_runs_plots_bounded_series_and_exports(qtbot, project, tmp_path) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    page = window.recordings_page
    window.navigate("recordings")
    settle(qtbot, window)
    assert page.runs.rowCount() == 1
    assert not page.recovery.isVisible()
    page.runs.selectRow(0)
    settle(qtbot, window)
    assert "1 reads (0 failed)" in page.run_detail.toPlainText()
    assert page.runs.item(0, 1).text() == "Targets complete"
    page.select_signal("372/0x1002/0..15")
    settle(qtbot, window)
    assert page.plot.source_points == 1
    assert "Constant value" in page.plot_summary.text()
    target = tmp_path / "out.csv"
    page.choose_export_path = lambda: str(target)
    page.export_button.click()
    settle(qtbot, window)
    assert target.read_text().count("\n") == 2


def test_bounded_reduction_keeps_plots_small() -> None:
    xs = list(range(1_000_000))
    series = bounded_minmax(xs, [float(value % 97) for value in xs], 1000)
    assert series.source_points == 1_000_000
    assert series.rendered_points <= 4000


def test_slow_project_work_does_not_block_the_window(qtbot, project) -> None:
    import threading
    import time

    from toyota_telemetry.operator import OperatorService

    root, _rows = project
    window = open_window(qtbot, root)
    release = threading.Event()
    original = OperatorService.tree

    def slow_tree(self, **kwargs):
        release.wait(5)
        return original(self, **kwargs)

    with mock.patch.object(OperatorService, "tree", slow_tree):
        started = time.monotonic()
        window.signals_page.rebuild()
        assert time.monotonic() - started < 0.5
        assert not window.context.worker.idle
        assert window.work_status.text() == "Working…"
        window.navigate("collect")
        qtbot.wait(50)
        release.set()
        settle(qtbot, window)
    assert window.work_status.text() == ""
    assert window.signals_page.tree.topLevelItemCount() == 1


def test_superseded_background_results_are_dropped(qtbot) -> None:
    from toyota_telemetry.ui.worker import ServiceWorker

    worker = ServiceWorker()
    shown = []
    for value in range(5):
        worker.submit("same", lambda value=value: value, shown.append)
    qtbot.waitUntil(lambda: worker.idle, timeout=5_000)
    assert shown == [4]


def test_new_signal_resets_the_plot_view_but_resizes_keep_zoom(qtbot) -> None:
    from toyota_telemetry.ui.widgets.metric_plot import MetricPlot

    plot = MetricPlot()
    qtbot.addWidget(plot)
    plot.resize(600, 300)
    plot.show()
    plot.set_series(bounded_minmax([0, 1, 2], [0.0, 1.0, 2.0], 600))
    plot.getPlotItem().getViewBox().setRange(xRange=(0.5, 1.0), yRange=(0.5, 1.0), padding=0)
    plot.set_series(bounded_minmax([0, 1, 2], [0.0, 1.5, 2.0], 600), reset_view=False)
    (x_low, x_high), _ = plot.getPlotItem().getViewBox().viewRange()
    assert (round(x_low, 2), round(x_high, 2)) == (0.5, 1.0)
    plot.set_series(bounded_minmax([0, 100, 200], [1000.0, 5000.0, 9000.0], 600), reset_view=True)
    qtbot.wait(50)
    (x_low, x_high), (y_low, y_high) = plot.getPlotItem().getViewBox().viewRange()
    assert x_low <= 0 and x_high >= 200
    assert y_low <= 1000 and y_high >= 9000


def test_signal_rows_show_one_status_ladder(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    data = window.signals_page.tree_data
    assert data.find_signal("372/0x1002/0..15")[2].status == "Reviewed"
    assert data.find_signal("372/0x1003/0..7")[2].status == "Not checked"
    assert data.find_signal("405/0x2001/0..7")[2].status == "Not checked"
    headers = [window.signals_page.tree.headerItem().text(c) for c in range(5)]
    assert headers == ["Endpoint / DID / Signal", "Status", "Interval", "Target", "Progress"]


def test_filter_unticks_rows_it_hides(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    page = window.signals_page
    page.check("signal", "372/0x1002/0..15")
    page.check("signal", "372/0x1003/0..7")
    page.state_filter.setCurrentText("Not verified")
    settle(qtbot, window)
    selection = page.checked_selection()
    assert selection.signals == ["372/0x1003/0..7"]
    page.state_filter.setCurrentText("All")
    settle(qtbot, window)
    assert page.checked_selection().signals == ["372/0x1003/0..7"]


def test_go_selects_the_item_the_next_step_is_about(qtbot, project, probes) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    page = window.signals_page
    page.check("did", f"{UNCONFIRMED_KEY}/0x2001")
    page.confirm_live = lambda dialog: True
    page.verify_button.click()
    wait_for_job(qtbot, page)
    settle(qtbot, window)
    assert window.next_text.text().startswith("Next: Confirm responding endpoint")
    page.tree.setCurrentItem(None)
    window.navigate("setup")
    window.next_button.click()
    settle(qtbot, window)
    assert window.pages.currentWidget() is page
    assert page._current() == ("endpoint", UNCONFIRMED_KEY)
    assert page.confirm_button.isEnabled()


def test_endpoint_dialogs_keep_input_and_explain_problems(qtbot, tmp_path) -> None:
    from toyota_telemetry.ui.dialogs import EndpointAddDialog, EndpointConfirmDialog

    add = EndpointAddDialog(["extended"])
    qtbot.addWidget(add)
    add.tester.setText("0x7D0")
    add.response.setText("0x7D8")
    add.accept()
    assert add.result() != add.DialogCode.Accepted
    assert add.error.text() == "Enter at least one ECU key."
    assert add.tester.text() == "0x7D0"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/probe.md").write_text("evidence\n")
    confirm = EndpointConfirmDialog("7a1-7a9-extended", [405], tmp_path)
    qtbot.addWidget(confirm)
    confirm.accept()
    assert confirm.error.text() == "Choose the evidence document for this endpoint."
    confirm.source.setText("docs/missing.md")
    confirm.accept()
    assert "does not exist" in confirm.error.text()
    confirm.source.setText("docs/probe.md")
    confirm.accept()
    assert confirm.result() == confirm.DialogCode.Accepted


def test_quarantine_asks_first_and_runs_list_interrupted_first(qtbot, project) -> None:
    from toyota_telemetry.capture_writer import NativeRunWriter

    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    signals = window.signals_page
    signals.check("signal", "372/0x1002/0..15")
    signals.run_dialog = lambda dialog: (dialog.set_field("minimum_samples", 9), True)[1]
    signals.target_button.click()
    settle(qtbot, window)
    signals.save_button.click()
    window.collect_page.check_readiness()
    settle(qtbot, window)
    plan_path = root / "scratch/plans/admitted-plan.json"
    plan = json.loads(plan_path.read_text())
    NativeRunWriter(
        root / "scratch/captures", plan, plan_path.read_bytes(), run_id="0" * 8 + "-0000-4000-8000-" + "0" * 12,
        created_utc="2026-10-01T10:00:00.123456Z", backend_kind="synthetic", disk_free_bytes=lambda _: 10**12,
    ).crash()
    window.context.changed()
    window.navigate("recordings")
    settle(qtbot, window)
    page = window.recordings_page
    assert page.runs.item(0, 1).text() == "Interrupted"
    assert page.runs.item(0, 0).text() == "2026-10-01 10:00 UTC"
    with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel) as asked:
        page.quarantine_button.click()
        settle(qtbot, window)
    assert asked.called
    assert page.runs.item(0, 1).text() == "Interrupted"
