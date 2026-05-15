"""DTMF / dial pad widget.

Each key is a custom QPushButton subclass that paints the digit big and
the letter sub-label small, so the keypad reads as digits-with-captions
instead of two same-sized lines stacked.

Public surface kept stable:
  - signals: digit_pressed(str), call_requested(str), hangup_requested
  - widgets: entry (QLineEdit), call_btn (QPushButton), hangup_btn (QPushButton)
  - method: set_in_call(bool)

The hosting shell can hide entry / call_btn / hangup_btn when it
provides those affordances elsewhere (PhoneShell does this -- the top
strip owns the dial input and Call button, so the dialpad becomes a
pure 3x4 numeric grid below).
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)

_KEYS = [
    ("1", ""),    ("2", "ABC"),  ("3", "DEF"),
    ("4", "GHI"), ("5", "JKL"),  ("6", "MNO"),
    ("7", "PQRS"),("8", "TUV"),  ("9", "WXYZ"),
    ("*", ""),    ("0", "+"),    ("#", ""),
]


class _KeyButton(QPushButton):
    """Dialpad key with two-tier text: big digit + small caption.

    Standard QPushButton text is single-tier; we paint the digit + caption
    ourselves in paintEvent so the QSS still drives the button chrome
    (background, hover, border) but the typography is custom.
    """

    def __init__(self, digit: str, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DialpadKey")
        # Empty native text so QPushButton doesn't draw a duplicate label.
        self.setText("")
        self._digit = digit
        self._caption = caption

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(60, 60)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        # Let the style draw bg + border + hover state via QSS.
        opt = QStyleOptionButton()
        opt.initFrom(self)
        opt.rect = self.rect()
        opt.text = ""
        opt.icon = self.icon()
        # `down` reflects pressed state for the CE_PushButton background
        opt.state |= QStyle.State_Sunken if self.isDown() else QStyle.State_Raised
        if self.isChecked():
            opt.state |= QStyle.State_On
        if self.isEnabled():
            opt.state |= QStyle.State_Enabled
        if self.underMouse():
            opt.state |= QStyle.State_MouseOver
        self.style().drawControl(QStyle.CE_PushButton, opt, QPainter(self), self)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Big digit
        digit_font = QFont(self.font())
        digit_font.setPointSize(20)
        digit_font.setWeight(QFont.Weight.Normal)
        painter.setFont(digit_font)
        digit_rect = self.rect().adjusted(0, 4, 0, -14 if self._caption else 0)
        painter.drawText(
            digit_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            self._digit,
        )
        # Caption (small caps under)
        if self._caption:
            cap_font = QFont(self.font())
            cap_font.setPointSize(7)
            cap_font.setWeight(QFont.Weight.Bold)
            cap_font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
            painter.setFont(cap_font)
            cap_rect = self.rect().adjusted(0, 0, 0, -4)
            painter.drawText(
                cap_rect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                self._caption,
            )
        painter.end()


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
            btn = _KeyButton(key, sub, self)
            btn.setMinimumSize(60, 60)
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
