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
from toyota_telemetry.ui.signals_page import ROLE


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
    assert [step.title for step in window.navigation.steps] == ["Setup", "Signals", "Collect", "Recordings"]
    assert window.navigation.steps[0].state == "next"
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
    assert json.loads((root / "config/catalog-manifest.json").read_text())["catalog_row_count"] == 4
    setup = window.setup_page
    setup.connector_kind.setCurrentIndex(setup.connector_kind.findData("slcan"))
    setup.connector_fields["slcan"]["port"].setText("COM7")
    setup.executable.setText("")
    setup.connector_save_button.click()
    saved = json.loads((root / "state/local-settings.json").read_text())
    assert saved["backend"] == {
        "kind": "slcan", "executable": None, "settings": {"port": "COM7"},
    }
    assert window.adapter_status.text() == "Adapter closed"


def test_signals_tree_verifies_checked_rows_after_confirmation(qtbot, project, probes) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    page = window.signals_page
    assert page.model.rowCount() == 2
    page.select("did", f"{UNCONFIRMED_KEY}/0x2001", add=True)
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
    page.select("signal", "372/0x1003/0..7", add=True)
    page.select("signal", "372/0x1003/8..15", add=True)

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
    page.select("signal", "372/0x1003/0..7", add=True)
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
    page.select("signal", "372/0x1003/0..7", add=True)
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
    signals.select("signal", "372/0x1002/0..15", add=True)
    signals.run_dialog = lambda dialog: (dialog.set_field("minimum_samples", 2), True)[1]
    signals.target_button.click()
    settle(qtbot, window)
    signals.save_button.click()
    settle(qtbot, window)
    collect.check_readiness()
    settle(qtbot, window)
    assert collect.readiness.ready, collect.readiness.problems
    # Progress lives here: one row per target, and the endpoint is named, not identified.
    assert collect.targets.topLevelItemCount() == 1
    item = collect.targets.topLevelItem(0)
    assert item.data(0, Qt.ItemDataRole.UserRole) == "372/0x1002/0..15"
    assert item.text(3) == "1 sample, 1 run"
    assert collect.targets_group.title() == "Targets: 0 of 1 met"
    assert collect.readiness.endpoint_id not in collect.endpoint.currentText()
    collect._open_target(item, 0)
    assert window.pages.currentWidget() is signals
    assert signals._current() == ("signal", "372/0x1002/0..15")
    window.navigate("collect")
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
    assert window.signals_page.model.rowCount() == 1


def test_superseded_background_results_are_dropped(qtbot) -> None:
    from toyota_telemetry.ui.worker import ServiceWorker

    worker = ServiceWorker()
    shown = []
    for value in range(5):
        worker.submit("same", lambda value=value: value, shown.append)
    qtbot.waitUntil(lambda: worker.idle, timeout=5_000)
    assert shown == [4]


class FakeWheel:
    def __init__(self, delta, horizontal=False, ctrl=False):
        self._delta, self._horizontal, self._ctrl = delta, horizontal, ctrl
        self.accepted = None

    def delta(self):
        return self._delta

    def orientation(self):
        return Qt.Orientation.Horizontal if self._horizontal else Qt.Orientation.Vertical

    def modifiers(self):
        return Qt.KeyboardModifier.ControlModifier if self._ctrl else Qt.KeyboardModifier.NoModifier

    def pos(self):
        from PySide6.QtCore import QPointF

        return QPointF(0, 0)

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


def test_plot_uses_dashboard_time_interaction(qtbot) -> None:
    from toyota_telemetry.ui.widgets.metric_plot import MetricPlot, RunSpan

    plot = MetricPlot()
    qtbot.addWidget(plot)
    plot.resize(600, 300)
    plot.show()
    xs = [0.0, 1.0, 2.0, 3.0, float("nan"), 4.0, 5.0, 6.0]
    ys = [1.0, 2.0, 3.0, 4.0, float("nan"), 10.0, 20.0, 30.0]
    runs = [RunSpan("aaaaaaaa-run", 0.0, 3.0), RunSpan("bbbbbbbb-run", 4.0, 6.0)]
    xs[4] = 3.5
    plot.set_data(xs, ys, runs)
    plot.set_measurement("Speed", "km/h")
    view = plot.getPlotItem().getViewBox()
    qtbot.wait(50)
    (low, high), (y_low, y_high) = view.viewRange()
    assert low <= 0 and high >= 6 and y_low <= 1 and y_high >= 30
    # Hover and click read the nearest finite sample, with its run.
    assert plot.nearest(3.4) == 3
    assert plot.describe(plot.nearest(5.2)) == "Speed: 20 km/h\nrun bbbbbbbb at 0:01"
    # Plain vertical scroll does not zoom; sideways scroll pans; Ctrl+scroll zooms time only.
    wheel = FakeWheel(120)
    view.wheelEvent(wheel)
    assert wheel.accepted is False and view.viewRange()[0] == [low, high]
    view.wheelEvent(FakeWheel(-120, horizontal=True))
    (pan_low, pan_high), _ = view.viewRange()
    assert pan_low > low and round(pan_high - pan_low, 6) == round(high - low, 6)
    view.wheelEvent(FakeWheel(240, ctrl=True))
    zoomed = view.viewRange()[0]
    assert zoomed[1] - zoomed[0] < high - low
    # A selected range zooms time; the value axis fits what is visible.
    view.setXRange(4.0, 6.0, padding=0)
    qtbot.wait(50)
    _, (y_low, y_high) = view.viewRange()
    assert y_low >= 5 and y_high <= 35
    plot.show_all()
    qtbot.wait(50)
    (low, high), _ = view.viewRange()
    assert low <= 0 and high >= 6
    assert plot.highlight_run("bbbbbbbb-run") and not plot.highlight_run("missing")


def test_step_bar_shows_progress_and_unsaved_changes(qtbot, project) -> None:
    from toyota_telemetry.ui.widgets.step_bar import ATTENTION, DONE

    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    settle(qtbot, window)
    steps = window.navigation.steps
    assert steps[0].state == DONE
    assert [part.label for part in steps[0].parts] == ["Connector", "Candidates"]
    assert steps[1].state in ("next", "pending")
    steps[3].click()
    assert window.pages.currentWidget() is window.recordings_page
    assert steps[3].isChecked() and not steps[0].isChecked()
    page = window.signals_page
    page.select("signal", "372/0x1003/0..7")
    page.run_dialog = lambda dialog: (dialog.set_field("minimum_samples", 5), True)[1]
    page.target_button.click()
    settle(qtbot, window)
    assert steps[1].state == ATTENTION
    assert "Unsaved" in [part.label for part in steps[1].parts]


def test_selection_panel_batch_edits_collection_settings(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    page = window.signals_page
    endpoint = next(row for row in page.tree_data.endpoints if row.confirmed)
    first, second = (did.key for did in endpoint.dids[:2])
    page.select("did", first)
    page.did_interval.setValue(500)
    assert page.did_apply.isEnabled()
    page.did_apply.click()
    settle(qtbot, window)
    page.select("did", first)
    page.select("did", second, add=True)
    assert page.selection_title.text() == "2 DIDs selected"
    assert page.did_interval.mixed and page.did_interval.text() == "mixed"
    assert not page.did_apply.isEnabled()
    page.did_priority.setValue(70)
    page.did_apply.click()
    settle(qtbot, window)
    settings = {did.key: did.settings for did in page.tree_data.endpoint(endpoint.key).dids}
    assert settings[first]["target_interval_ms"] == 500 and settings[second]["target_interval_ms"] == 1000
    assert settings[first]["priority"] == settings[second]["priority"] == 70
    # Selecting the endpoint stands for all of its DIDs and signals.
    page.select("endpoint", endpoint.key)
    assert len(page.selection().dids) == len(endpoint.dids)
    assert page.collection_group.title() == f"Collection: {len(endpoint.dids)} DIDs"


def test_signal_rows_show_one_status_ladder(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    data = window.signals_page.tree_data
    assert data.find_signal("372/0x1002/0..15")[2].status == "Reviewed"
    assert data.find_signal("372/0x1003/0..7")[2].status == "Not checked"
    assert data.find_signal("405/0x2001/0..7")[2].status == "Not checked"
    model = window.signals_page.model
    headers = [model.headerData(column, Qt.Orientation.Horizontal) for column in range(model.columnCount())]
    assert headers == ["Name", "Readable", "Collect", "Every", "Late after", "Priority", "Target"]


def test_plan_table_names_ecus_and_uses_one_readable_vocabulary(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    page = window.signals_page
    model = page.model
    readable = set()
    for row in range(model.rowCount()):
        endpoint = page.tree_data.endpoint(model.item(row, 0).data(ROLE)[1])
        label = model.item(row, 0).text()
        # The ECU name or key leads; the ID (session, hash) stays in the tooltip.
        assert endpoint.key not in label
        assert endpoint.key in model.item(row, 0).toolTip()
        for child in range(model.item(row, 0).rowCount()):
            readable.add(model.item(row, 0).child(child, 1).text())
    assert readable <= {"✓ Readable", "? Not checked", "✕ Refused"}
    assert "Progress" not in [model.headerData(c, Qt.Orientation.Horizontal) for c in range(model.columnCount())]


def test_filter_menu_groups_combine(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    page = window.signals_page
    button = page.filter_button
    assert set(button.groups) == {"Readable", "Collect", "Target", "ECU"}
    assert button.text() == "Filter" and not page.clear_filters_button.isEnabled()
    button.actions_["targeted"].setChecked(True)
    button.actions_["untargeted"].setChecked(True)
    settle(qtbot, window)
    # Options in one group widen the result: with and without a target is everything.
    assert button.text() == "Filter (2)"
    assert len(list(page._proxy_rows())) == page.model.rowCount() + sum(
        1 + len(did.signals) for endpoint in page.tree_data.endpoints for did in endpoint.dids
    )
    ecu = next(key for key in button.actions_ if key.startswith("ecu:"))
    button.actions_[ecu].setChecked(True)
    settle(qtbot, window)
    shown = {key for (kind, key), _index in page._proxy_rows() if kind == "endpoint"}
    assert shown == {ecu[4:]}
    page.clear_filters_button.click()
    settle(qtbot, window)
    assert button.count() == 0 and button.text() == "Filter"


def test_filter_unticks_rows_it_hides(qtbot, project) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    page = window.signals_page
    page.select("signal", "372/0x1002/0..15", add=True)
    page.select("signal", "372/0x1003/0..7", add=True)
    page.filter_button.actions_["unchecked"].setChecked(True)
    settle(qtbot, window)
    assert page.selection().signals == ["372/0x1003/0..7"]
    page.filter_button.actions_["unchecked"].setChecked(False)
    settle(qtbot, window)
    assert page.selection().signals == ["372/0x1003/0..7"]


def test_go_selects_the_item_the_next_step_is_about(qtbot, project, probes) -> None:
    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    page = window.signals_page
    page.select("did", f"{UNCONFIRMED_KEY}/0x2001", add=True)
    page.confirm_live = lambda dialog: True
    page.verify_button.click()
    wait_for_job(qtbot, page)
    settle(qtbot, window)
    assert window.next_text.text().startswith("Next: Confirm responding endpoint")
    page.view.clearSelection()
    window.navigate("setup")
    window.next_button.click()
    settle(qtbot, window)
    assert window.pages.currentWidget() is page
    assert page._current() == ("endpoint", UNCONFIRMED_KEY)
    assert page.confirm_button.isEnabled()


def test_endpoint_dialogs_keep_input_and_explain_problems(qtbot) -> None:
    from toyota_telemetry.ui.dialogs import EndpointAddDialog, EndpointConfirmDialog

    add = EndpointAddDialog(["extended"])
    qtbot.addWidget(add)
    add.tester.setText("0x7D0")
    add.response.setText("0x7D8")
    add.accept()
    assert add.result() != add.DialogCode.Accepted
    assert add.error.text() == "Enter at least one ECU key."
    assert add.tester.text() == "0x7D0"
    confirm = EndpointConfirmDialog("7a1-7a9-extended", [405])
    qtbot.addWidget(confirm)
    confirm.accept()
    assert confirm.error.text() == "Enter where the evidence for this endpoint is written up."
    confirm.source.setText("C:/notes/probe.md")
    confirm.accept()
    assert "relative path in the deliverable repository" in confirm.error.text()
    confirm.source.setText("captures/run")
    confirm.accept()
    assert confirm.result() != confirm.DialogCode.Accepted
    # Evidence lives in the deliverable repository, so it is not looked up here.
    confirm.source.setText("docs/evidence/probe.md")
    confirm.accept()
    assert confirm.result() == confirm.DialogCode.Accepted


def test_quarantine_asks_first_and_runs_list_interrupted_first(qtbot, project) -> None:
    from toyota_telemetry.capture_writer import NativeRunWriter

    root, rows = project
    window = open_window(qtbot, root)
    import_rows(qtbot, window, rows)
    signals = window.signals_page
    signals.select("signal", "372/0x1002/0..15", add=True)
    signals.run_dialog = lambda dialog: (dialog.set_field("minimum_samples", 9), True)[1]
    signals.target_button.click()
    settle(qtbot, window)
    signals.save_button.click()
    window.collect_page.check_readiness()
    settle(qtbot, window)
    plan_path = root / "state/plans/admitted-plan.json"
    plan = json.loads(plan_path.read_text())
    NativeRunWriter(
        root / "captures", plan, plan_path.read_bytes(), run_id="0" * 8 + "-0000-4000-8000-" + "0" * 12,
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


def test_new_project_and_export_result(qtbot, tmp_path) -> None:
    window = open_window(qtbot)
    workspace = tmp_path / "new-car"
    window.choose_new_folder = lambda: str(workspace)
    window.context.actions["init"].trigger()
    settle(qtbot, window)
    assert window.project_path == workspace.resolve()
    assert window.next_text.text() == "Next: Configure the connector"
    deliverable = tmp_path / "deliverable"
    page = window.recordings_page
    starts = []
    page.choose_result_folder = lambda start: (starts.append(start), str(deliverable))[1]
    window.navigation.setCurrentRow(3)
    page.result_button.click()
    settle(qtbot, window)
    assert page.result_text.text().startswith("Exported result")
    assert (deliverable / "result/current.json").is_file()
    assert (deliverable / "config/transport.json").read_bytes() == (workspace / "config/transport.json").read_bytes()
    page.result_button.click()
    settle(qtbot, window)
    assert starts == [str(tmp_path), deliverable.as_posix()]
