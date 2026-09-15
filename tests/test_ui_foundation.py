from __future__ import annotations

import threading

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import QPushButton

from toyota_telemetry.ui.shell import OperatorWindow
from toyota_telemetry.ui.tasks import LatestTaskController


def test_shell_starts_offline_without_project_or_adapter(qtbot) -> None:
    window = OperatorWindow(restore_settings=False)
    qtbot.addWidget(window)
    window.show()

    assert window.project_path is None
    assert window.navigation.count() == 7
    assert window.transport_status.text() == "Offline"
    assert window.adapter_status.text() == "Adapter closed"
    assert window.signal_list_page.model.rowCount() == 10_000
    assert window._splitter.count() == 2
    assert window.signal_list_page.source.count() == 2
    assert [window.navigation.item(row).text() for row in range(7)] == [
        "Overview",
        "Connect hardware",
        "Build signal list",
        "Verify with vehicle",
        "Set collection targets",
        "Collect data",
        "Analyze recordings",
    ]


def test_evidence_plot_receives_bounded_million_point_result(qtbot) -> None:
    window = OperatorWindow(restore_settings=False)
    qtbot.addWidget(window)
    window.show()
    window.navigation.setCurrentRow(6)

    evidence = window.analyze_page.evidence_view

    qtbot.waitUntil(
        lambda: "1,000,000 synthetic source points" in evidence.status.text(),
        timeout=10_000,
    )
    assert evidence.plot.rendered_points <= 8_000
    assert "1,000,000 synthetic source points" in evidence.status.text()


def test_preview_navigation_and_connector_controls_are_interactive(qtbot) -> None:
    window = OperatorWindow(restore_settings=False)
    qtbot.addWidget(window)
    window.show()

    window.overview_page.cards[0].action.click()
    assert window.navigation.currentRow() == 1
    assert window.pages.currentWidget() is window.connect_page

    window.connect_page.connector.setCurrentText("gs_usb")
    assert window.connect_page.connector_settings.currentIndex() == 2


def test_target_and_verification_views_group_by_endpoint_and_did(qtbot) -> None:
    window = OperatorWindow(restore_settings=False)
    qtbot.addWidget(window)

    assert window.verify_page.tree.topLevelItemCount() == 2
    assert window.verify_page.tree.topLevelItem(0).childCount() == 3
    assert window.targets_page.tree.topLevelItemCount() == 5
    assert window.targets_page.did_groups["F421"].childCount() == 4
    assert window.targets_page.did_groups["F421"].checkState(0) == Qt.CheckState.Checked
    assert (
        window.targets_page.did_groups["F421"].child(0).checkState(0)
        == Qt.CheckState.Checked
    )


def test_preview_field_checks_never_claim_real_validation_or_import(qtbot) -> None:
    window = OperatorWindow(restore_settings=False)
    qtbot.addWidget(window)

    window.connect_page.connector.setCurrentText("J2534")
    window.connect_page._validate_preview()
    assert "enter PassThru DLL" in window.connect_page.feedback.text()
    window.connect_page.j2534_dll.setText("C:/vendor/passthru.dll")
    window.connect_page._validate_preview()
    assert "self-test have not run" in window.connect_page.feedback.text()

    window.signal_list_page.source.setCurrentText("Normalized file")
    window.signal_list_page.preview_button.click()
    assert "enter File" in window.signal_list_page.summary.text()
    window.signal_list_page.candidate_file.setText("C:/examples/signals.json")
    window.signal_list_page.preview_button.click()
    assert "not parsed" in window.signal_list_page.summary.text()
    assert window.signal_list_page.example_table.rowCount() == 9
    assert window.signal_list_page.model.rowCount() == 10_000
    window.signal_list_page.search.setText("Battery current")
    assert sum(
        not window.signal_list_page.example_table.isRowHidden(row)
        for row in range(window.signal_list_page.example_table.rowCount())
    ) == 1


def test_selection_filters_and_readiness_navigation_work_in_mock(qtbot) -> None:
    window = OperatorWindow(restore_settings=False)
    qtbot.addWidget(window)
    window.show()

    window.targets_page.tree.setCurrentItem(window.targets_page.signal_items["Coolant temperature"])
    assert window.targets_page.definition_name.text() == "Coolant temperature"
    assert window.targets_page.minimum_samples.value() == 12
    window.targets_page.minimum_samples.setValue(20)
    window.targets_page.tree.setCurrentItem(window.targets_page.signal_items["Wheel speed FL"])
    window.targets_page.tree.setCurrentItem(window.targets_page.signal_items["Coolant temperature"])
    assert window.targets_page.minimum_samples.value() == 20
    assert "draft" in window.targets_page.signal_items["Coolant temperature"].text(2)
    window.targets_page.tree.setCurrentItem(window.targets_page.signal_items["Wheel speed FL"])
    window.targets_page.did_target.setValue(120)
    window.targets_page.tree.setCurrentItem(window.targets_page.signal_items["Wheel speed FR"])
    assert window.targets_page.did_target.value() == 120
    assert "120 / 250 ms" in window.targets_page.did_groups["F421"].text(2)

    window.verify_page.state_filter.setCurrentText("Needs verification")
    assert window.verify_page.did_items["F190"].isHidden()
    assert not window.verify_page.did_items["F423"].isHidden()
    window.verify_page.did_items["F423"].setCheckState(0, Qt.CheckState.Unchecked)
    assert "2 DIDs" in window.verify_page.scope_label.text()

    window.navigation.setCurrentRow(5)
    window.collect_page.issue_requested.emit("F423")
    assert window.navigation.currentRow() == 3
    assert window.verify_page.tree.currentItem() is window.verify_page.did_items["F423"]
    window.verify_page.return_button.click()
    assert window.navigation.currentRow() == 5

    window.collect_page.issue_requested.emit("Coolant validity")
    assert window.navigation.currentRow() == 4
    assert window.targets_page.definition_name.text() == "Coolant validity"
    window.targets_page.return_button.click()
    assert window.navigation.currentRow() == 5


def test_analysis_mock_selection_and_filters_change_the_display(qtbot) -> None:
    window = OperatorWindow(restore_settings=False)
    qtbot.addWidget(window)
    window.show()
    window.navigation.setCurrentRow(6)
    evidence = window.analyze_page.evidence_view

    evidence.signals.setCurrentRow(3)
    assert "Brake switch" in evidence.selection_status.text()
    evidence.run_filter.setCurrentIndex(1)
    qtbot.waitUntil(
        lambda: evidence.plot.source_points < 1_000_000 and evidence.plot.source_points > 0,
        timeout=10_000,
    )
    evidence.quality_toggle.setChecked(False)
    assert not evidence.quality_message.isVisible()
    window.analyze_page.run_chooser.setCurrentIndex(1)
    assert "Completed" in window.analyze_page.run_result.text()


def test_vehicle_and_project_mutating_preview_actions_are_disabled(qtbot) -> None:
    window = OperatorWindow(restore_settings=False)
    qtbot.addWidget(window)

    disabled_actions = {
        button.text(): button
        for button in window.findChildren(QPushButton)
        if button.text()
        in {
            "Use this candidate list",
            "Verify 2 endpoints and 3 DIDs (Live)",
            "Review and start collection",
            "Stop safely",
            "Save local settings",
            "Save signal review and collection targets",
        }
    }
    assert len(disabled_actions) == 6
    assert all(not button.isEnabled() for button in disabled_actions.values())


def test_latest_task_controller_drops_superseded_result(qtbot) -> None:
    pool = QThreadPool()
    pool.setMaxThreadCount(2)
    controller = LatestTaskController(pool)
    observed: list[str] = []
    controller.result.connect(observed.append)
    release_old = threading.Event()

    def old_work(cancel: threading.Event) -> str:
        release_old.wait(2)
        return "old"

    controller.start(old_work)
    controller.start(lambda cancel: "new")
    qtbot.waitUntil(lambda: observed == ["new"], timeout=5_000)
    release_old.set()
    assert pool.waitForDone(5_000)
    qtbot.wait(20)
    assert observed == ["new"]
