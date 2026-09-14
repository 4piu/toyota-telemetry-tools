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
        lambda: "1,000,000 source points" in evidence.status.text(),
        timeout=10_000,
    )
    assert evidence.plot.rendered_points <= 8_000
    assert "1,000,000 source points" in evidence.status.text()


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
    assert window.targets_page.tree.topLevelItemCount() == 2
    assert window.targets_page.tree.topLevelItem(1).childCount() == 4
    assert window.targets_page.tree.topLevelItem(1).checkState(0) == Qt.CheckState.Checked
    assert (
        window.targets_page.tree.topLevelItem(1).child(0).checkState(0)
        == Qt.CheckState.Checked
    )


def test_vehicle_and_project_mutating_preview_actions_are_disabled(qtbot) -> None:
    window = OperatorWindow(restore_settings=False)
    qtbot.addWidget(window)

    disabled_actions = {
        button.text(): button
        for button in window.findChildren(QPushButton)
        if button.text()
        in {
            "Use this candidate list",
            "Verify 2 endpoints and 4 DIDs (Live)",
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
