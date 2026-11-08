"""Setup: the CAN adapter on this computer. Project actions live in the Project menu."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from toyota_telemetry.i18n import _
from toyota_telemetry.operator.connector import CONNECTOR_FIELDS, CONNECTOR_LABELS

from .context import PageContext
from .widgets import button_row, notice, page_title, path_field, set_enabled, show_notice


class SetupPage(QWidget):
    def __init__(self, context: PageContext) -> None:
        super().__init__()
        self.context = context
        layout = QVBoxLayout(self)
        layout.addWidget(page_title(
            _("Setup"),
            _("The CAN adapter on this computer; nothing here transmits"),
        ))
        layout.addWidget(self._connector_group())
        layout.addStretch(1)
        context.project_changed.connect(self.refresh)
        self.refresh()

    # Connector --------------------------------------------------------------

    def _connector_group(self) -> QGroupBox:
        group = QGroupBox(_("Connector"))
        group.setToolTip(_("CAN adapter on this computer; never exported"))
        layout = QVBoxLayout(group)
        form = QFormLayout()
        self.connector_kind = QComboBox()
        for kind, label in CONNECTOR_LABELS.items():
            self.connector_kind.addItem(label, kind)
        form.addRow(_("Connector"), self.connector_kind)
        self.connector_fields: dict[str, dict[str, QLineEdit]] = {}
        self.connector_stack = QStackedWidget()
        for kind, fields in CONNECTOR_FIELDS.items():
            page = QWidget()
            page_form = QFormLayout(page)
            page_form.setContentsMargins(0, 0, 0, 0)
            editors = {}
            for name, label, _required in fields:
                # Field labels are English constants of the connector module.
                label = _(label)
                editor = QLineEdit()
                editor.setAccessibleName(label)
                editors[name] = editor
                page_form.addRow(label, path_field(editor, label) if name == "dll" else editor)
            self.connector_fields[kind] = editors
            self.connector_stack.addWidget(page)
        form.addRow(self.connector_stack)
        self.executable = QLineEdit()
        self.executable.setPlaceholderText(_("default"))
        self.executable.setToolTip(_("Empty uses the bundled worker"))
        form.addRow(_("Worker executable"), self.executable)
        layout.addLayout(form)
        self.context.register("connector.show", self.connector_kind, _("Adapter type"))
        self.connector_kind.currentIndexChanged.connect(self.connector_stack.setCurrentIndex)
        self.connector_check_button = QPushButton(_("Check"))
        self.connector_save_button = QPushButton(_("Save"))
        self.context.register(
            "connector.check", self.connector_check_button,
            _("Self-test; the adapter is not opened"),
        )
        self.context.register("connector.set", self.connector_save_button, _("Saved on this computer"))
        self.connector_check_button.clicked.connect(self.check_connector)
        self.connector_save_button.clicked.connect(self.save_connector)
        layout.addLayout(button_row(self.connector_check_button, self.connector_save_button))
        self.connector_result = notice()
        layout.addWidget(self.connector_result)
        return group

    def _connector_document(self):
        kind = self.connector_kind.currentData()
        settings = {name: editor.text() for name, editor in self.connector_fields[kind].items()}
        return self.context.service.connector_document(kind, settings, self.executable.text().strip() or None)

    def check_connector(self) -> None:
        if self.context.service is None:
            return
        try:
            document = self._connector_document()
        except ValueError as exc:
            show_notice(self.connector_result, _("Problem: {problem}").format(problem=exc))
            return

        def done(check) -> None:
            self.connector_check_button.setEnabled(True)
            lines = [_("OK: {note}").format(note=note) for note in check.notes]
            lines += [_("Problem: {problem}").format(problem=text) for text in check.problems]
            if check.ok:
                lines.append(_("Settings are valid."))
            show_notice(self.connector_result, "\n".join(lines))

        def failed(detail: str) -> None:
            self.connector_check_button.setEnabled(True)
            self.context.error(_("Connector check failed"), _first(detail))

        show_notice(self.connector_result, _("Checking…"))
        self.connector_check_button.setEnabled(False)
        self.context.run(
            "setup.connector.check", lambda service: service.check_connector(document), done, failed,
        )

    def save_connector(self) -> None:
        if self.context.service is None:
            return
        try:
            document = self._connector_document()
            self.context.service.save_connector(document)
        except (OSError, ValueError) as exc:
            show_notice(self.connector_result, _("Not saved: {error}").format(error=exc))
            return
        show_notice(self.connector_result, _("Saved."))
        self.context.changed()

    def load_connector(self, document) -> None:
        for editors in self.connector_fields.values():
            for editor in editors.values():
                editor.clear()
        self.executable.clear()
        if document is None:
            return
        backend = document["backend"]
        self.connector_kind.setCurrentIndex(self.connector_kind.findData(backend["kind"]))
        for name, value in backend["settings"].items():
            self.connector_fields[backend["kind"]][name].setText(value)
        self.executable.setText(backend["executable"] or "")

    # Refresh ----------------------------------------------------------------

    def refresh(self) -> None:
        has_project = self.context.service is not None
        for widget in (self.connector_check_button, self.connector_save_button):
            set_enabled(widget, has_project, _("Open a project first."))
        show_notice(self.connector_result, "")
        self.load_connector(None)
        if has_project:
            self.context.run("setup.connector.load", lambda service: service.connector(), self.load_connector)


def _first(detail: str) -> str:
    return detail.split("\n\n", 1)[0]
