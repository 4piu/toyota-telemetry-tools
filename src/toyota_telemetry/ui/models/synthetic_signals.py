"""Virtual signal table used to prove the Foundation slice at realistic scale."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt


class SyntheticSignalModel(QAbstractTableModel):
    COLUMNS = (
        "Source",
        "ECU",
        "Endpoint",
        "DID",
        "Signal",
        "Bits",
        "Unit",
        "Decode candidate",
        "Import status",
    )

    def __init__(self, rows: int = 10_000) -> None:
        super().__init__()
        if rows < 0:
            raise ValueError("row count must not be negative")
        self._rows = rows

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else self._rows

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object | None:
        if not index.isValid() or not 0 <= index.row() < self._rows:
            return None
        if role not in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.ToolTipRole,
            Qt.ItemDataRole.AccessibleTextRole,
        ):
            return None
        value = self._value(index.row(), index.column())
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"Synthetic prototype value: {value}"
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{self.COLUMNS[index.column()]}: {value}"
        return value

    @staticmethod
    def _value(row: int, column: int) -> str | int:
        endpoint = row // 768
        did = 0x1000 + row // 4
        bit_start = (row % 4) * 8
        values: tuple[str | int, ...] = (
            ("TIS database", "Normalized file")[row % 2],
            ("Powertrain", "Hybrid control", "Body", "Chassis")[endpoint % 4],
            f"endpoint-{endpoint + 1:02d}",
            f"0x{did:04X}",
            f"Candidate signal {row + 1:05d}",
            f"{bit_start}..{bit_start + 7}",
            ("rpm", "km/h", "C", "%")[row % 4],
            ("raw", "raw * 0.01", "raw - 40", "bit")[row % 4],
            ("New", "Matches existing", "Needs review")[row % 3],
        )
        return values[column]
