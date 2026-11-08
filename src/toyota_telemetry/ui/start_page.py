"""Start screen: shown while no project is open."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from .widgets import button_row, page_title


class StartPage(QWidget):
    """Recent projects plus New and Open; everything else needs a project."""

    open_requested = Signal(Path)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 36, 48, 36)
        layout.addWidget(page_title(
            "Telemetry Operator",
            "A project is a folder: settings, candidates, recordings",
        ))
        self.new_button = QPushButton("New project")
        self.new_button.setToolTip("Create a workspace in an empty folder")
        self.open_button = QPushButton("Open project")
        self.open_button.setToolTip("Open an existing workspace folder")
        layout.addLayout(button_row(self.new_button, self.open_button))
        self.recent_label = QLabel("Recent projects")
        font = self.recent_label.font()
        font.setBold(True)
        self.recent_label.setFont(font)
        layout.addWidget(self.recent_label)
        self.recent = QListWidget()
        self.recent.setAccessibleName("Recent projects")
        self.recent.setToolTip("Double-click to open")
        self.recent.itemActivated.connect(
            lambda item: self.open_requested.emit(Path(item.data(Qt.ItemDataRole.UserRole)))
        )
        layout.addWidget(self.recent, 1)

    def set_recent(self, paths: list[Path]) -> None:
        self.recent.clear()
        for path in paths:
            item = QListWidgetItem(f"{path.name}    {path.parent}")
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            self.recent.addItem(item)
        self.recent_label.setVisible(bool(paths))
        self.recent.setVisible(bool(paths))
