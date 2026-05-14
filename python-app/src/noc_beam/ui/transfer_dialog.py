"""Small dialog for choosing a transfer kind + target.

Returns (target_uri, kind) where kind is one of "blind" | "attended".
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QButtonGroup,
    QGroupBox,
)


class TransferDialog(QDialog):
    def __init__(self, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("Transfer call")
        self.setMinimumWidth(360)

        self.target = QLineEdit()
        self.target.setPlaceholderText("Number or SIP URI")

        form = QFormLayout()
        form.addRow("Transfer to", self.target)

        kind_group = QGroupBox("Kind")
        self.kind_blind = QRadioButton("Blind — REFER immediately")
        self.kind_attended = QRadioButton("Attended — call target first, then complete")
        self.kind_blind.setChecked(True)
        group_box = QVBoxLayout(kind_group)
        group_box.addWidget(self.kind_blind)
        group_box.addWidget(self.kind_attended)

        # Mutually-exclusive radio group
        self._kind = QButtonGroup(self)
        self._kind.addButton(self.kind_blind)
        self._kind.addButton(self.kind_attended)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(kind_group)
        layout.addWidget(buttons)

    def result_target(self) -> str:
        return self.target.text().strip()

    def result_kind(self) -> str:
        return "attended" if self.kind_attended.isChecked() else "blind"
