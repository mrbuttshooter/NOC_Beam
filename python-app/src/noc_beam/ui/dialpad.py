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
        self.setObjectName("DialPadButton")
        # Empty native text so QPushButton doesn't draw a duplicate label.
        self.setText("")
        self._digit = digit
        self._caption = caption
        label = f"Dial {digit}" if digit not in ("*", "#") else f"Dial {digit} key"
        self.setAccessibleName(label)
        if caption:
            self.setAccessibleDescription(f"Dialpad key {digit}, letters {caption}")
        else:
            self.setAccessibleDescription(f"Dialpad key {digit}")
        # Visual 2.0 motion: quick opacity dip on press. Decoration only --
        # the DTMF/dial signal path does not depend on it.
        self.pressed.connect(self._press_flash)

    def _press_flash(self) -> None:
        try:
            from noc_beam.ui import motion

            motion.press_flash(self)
        except Exception:
            pass

    def sizeHint(self) -> QSize:  # noqa: N802
        # Visual 2.0: keys are proper tiles (brief: 52-56px tall) so the
        # keypad reads as the primary instrument, not an afterthought.
        return QSize(56, 52)

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
        # Big digit -- Visual 2.0 type scale: dialpad digit 20px, centered.
        digit_font = QFont(self.font())
        digit_font.setPixelSize(20)
        digit_font.setWeight(QFont.Weight.Medium)
        painter.setFont(digit_font)
        digit_rect = self.rect().adjusted(0, 2, 0, -14 if self._caption else 0)
        painter.drawText(
            digit_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            self._digit,
        )
        # Caption: 10px muted, letterspaced, visibly subordinate.
        if self._caption:
            cap_font = QFont(self.font())
            cap_font.setPixelSize(10)
            cap_font.setWeight(QFont.Weight.DemiBold)
            painter.setPen(self.palette().mid().color())
            cap_font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
            painter.setFont(cap_font)
            cap_rect = self.rect().adjusted(0, 0, 0, -7)
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
        self.setObjectName("DialPad")

        self.entry = QLineEdit()
        self.entry.setObjectName("DialpadEntry")
        self.entry.setPlaceholderText("Enter number or SIP URI")
        self.entry.setAccessibleName("Dial target")
        self.entry.setAlignment(Qt.AlignCenter)
        f = QFont()
        f.setPointSize(18)
        self.entry.setFont(f)
        self.entry.returnPressed.connect(self._on_call)

        grid = QGridLayout()
        # Visual 2.0: the grid breathes -- 8px gaps, equal margins.
        grid.setSpacing(8)
        grid.setContentsMargins(0, 0, 0, 0)
        for i, (key, sub) in enumerate(_KEYS):
            btn = _KeyButton(key, sub, self)
            btn.setMinimumSize(48, 52)
            btn.setMaximumHeight(56)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            # A11y: screen-readers announced "key 1, key 2..." with no
            # context; now each gets "Dial 1", "Dial 2", etc. The 12
            # keypad buttons + Call + Hangup + dial input cover the
            # primary calling task surface.
            btn.setAccessibleName(f"Dial {key}")
            if sub:
                btn.setAccessibleDescription(f"Key {key} ({sub})")
            btn.clicked.connect(lambda _=False, k=key: self._press(k))
            grid.addWidget(btn, i // 3, i % 3)

        actions = QHBoxLayout()
        self.call_btn = QPushButton("Call")
        self.call_btn.setObjectName("CallButton")
        self.call_btn.setAccessibleName("Place call")
        self.call_btn.setMinimumHeight(44)
        self.call_btn.clicked.connect(self._on_call)
        self.hangup_btn = QPushButton("Hang up")
        self.hangup_btn.setObjectName("HangupButton")
        self.hangup_btn.setAccessibleName("Hang up call")
        self.hangup_btn.setMinimumHeight(44)
        self.hangup_btn.clicked.connect(self.hangup_requested.emit)
        self.hangup_btn.setEnabled(False)
        actions.addWidget(self.call_btn)
        actions.addWidget(self.hangup_btn)

        layout = QVBoxLayout(self)
        # Zero side margins so the tile grid shares left/right edges with
        # the dial field + recents (the hosting page owns the 12px gutters).
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(8)
        layout.addWidget(self.entry)
        layout.addLayout(grid)
        layout.addLayout(actions)

    def set_in_call(self, in_call: bool) -> None:
        self.call_btn.setEnabled(not in_call)
        self.hangup_btn.setEnabled(in_call)

    # ------------------------------------------------------------------
    def _press(self, key: str) -> None:
        # When in a call the keys send DTMF tones, not dial-string
        # input. Previously the digit was ALSO appended to the
        # entry field, so by call-end the entry was littered with
        # every DTMF tone the user pressed mid-call (e.g. IVR
        # menu navigation left "12345" in the dial bar).
        if self.call_btn.isEnabled():
            self.entry.setText(self.entry.text() + key)
        self.digit_pressed.emit(key)

    def _on_call(self) -> None:
        target = self.entry.text().strip()
        if target:
            self.call_requested.emit(target)
