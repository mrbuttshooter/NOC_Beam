"""DTMF / dial pad widget."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_KEYS = [
    ("1", ""),    ("2", "ABC"),  ("3", "DEF"),
    ("4", "GHI"), ("5", "JKL"),  ("6", "MNO"),
    ("7", "PQRS"),("8", "TUV"),  ("9", "WXYZ"),
    ("*", ""),    ("0", "+"),    ("#", ""),
]


class DialPad(QWidget):
    """A 4x3 grid dial pad with a number entry field and Call / Hang-up buttons."""

    digit_pressed = Signal(str)
    call_requested = Signal(str)        # full target string
    hangup_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.entry = QLineEdit()
        self.entry.setObjectName("DialpadEntry")
        self.entry.setPlaceholderText("Enter number or SIP URI")
        self.entry.setAlignment(Qt.AlignCenter)
        f = QFont()
        f.setPointSize(18)
        self.entry.setFont(f)
        self.entry.returnPressed.connect(self._on_call)

        grid = QGridLayout()
        grid.setSpacing(6)
        for i, (key, sub) in enumerate(_KEYS):
            btn = QPushButton(f"{key}\n{sub}" if sub else key)
            btn.setObjectName("DialpadKey")
            btn.setMinimumSize(60, 56)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            btn.clicked.connect(lambda _=False, k=key: self._press(k))
            grid.addWidget(btn, i // 3, i % 3)

        actions = QHBoxLayout()
        self.call_btn = QPushButton("Call")
        self.call_btn.setObjectName("CallButton")
        self.call_btn.setMinimumHeight(44)
        self.call_btn.clicked.connect(self._on_call)
        self.hangup_btn = QPushButton("Hang up")
        self.hangup_btn.setObjectName("HangupButton")
        self.hangup_btn.setMinimumHeight(44)
        self.hangup_btn.clicked.connect(self.hangup_requested.emit)
        self.hangup_btn.setEnabled(False)
        actions.addWidget(self.call_btn)
        actions.addWidget(self.hangup_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.entry)
        layout.addLayout(grid)
        layout.addLayout(actions)

    def set_in_call(self, in_call: bool) -> None:
        self.call_btn.setEnabled(not in_call)
        self.hangup_btn.setEnabled(in_call)

    # ------------------------------------------------------------------
    def _press(self, key: str) -> None:
        self.entry.setText(self.entry.text() + key)
        self.digit_pressed.emit(key)

    def _on_call(self) -> None:
        target = self.entry.text().strip()
        if target:
            self.call_requested.emit(target)
