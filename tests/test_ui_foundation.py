from __future__ import annotations

import threading

from PySide6.QtCore import QThreadPool

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
    assert window.curate_page.model.rowCount() == 10_000


def test_evidence_plot_receives_bounded_million_point_result(qtbot) -> None:
    window = OperatorWindow(restore_settings=False)
    qtbot.addWidget(window)
    window.show()
    window.navigation.setCurrentRow(5)

    qtbot.waitUntil(lambda: window.evidence_page.plot.source_points == 1_000_000, timeout=10_000)
    assert window.evidence_page.plot.rendered_points <= 8_000
    assert "1,000,000 source points" in window.evidence_page.status.text()


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
