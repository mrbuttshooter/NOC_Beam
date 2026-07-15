"""NOC_Beam fluent-clean POC v2 -- the showcase pass.

Adds over v1: Mica backdrop, card-based dial surface, two-tier keypad
tiles, a live in-call hero card (as it would look mid-call), nav badge.
Still zero SIP wiring -- pure look-approval artifact.

Run:  .venv\\Scripts\\python.exe prototype\\fluent_poc_v2.py
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    FluentIcon,
    InfoBadge,
    InfoBadgePosition,
    LineEdit,
    MSFluentWindow,
    NavigationItemPosition,
    PrimaryPushButton,
    PushButton,
    SimpleCardWidget,
    StrongBodyLabel,
    Theme,
    ToolButton,
    TransparentPushButton,
    TransparentToolButton,
    setTheme,
    setThemeColor,
)

KEYS = [
    ("1", ""), ("2", "ABC"), ("3", "DEF"),
    ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
    ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"),
    ("*", ""), ("0", "+"), ("#", ""),
]

RECENTS = [
    ("96170010600", "Answered", "success", "17:06"),
    ("35796109901", "Cancelled", "error", "10:47"),
    ("96899406599", "Not found", "error", "11:48"),
]

MONO = QFont("Cascadia Mono")
MONO.setPointSize(11)


class KeyTile(SimpleCardWidget):
    def __init__(self, digit: str, caption: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(44)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(0)
        d = QLabel(digit, self)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont(self.font())
        f.setPointSize(13)
        f.setWeight(QFont.Weight.Medium)
        d.setFont(f)
        lay.addWidget(d)
        if caption:
            c = CaptionLabel(caption, self)
            c.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cf = QFont(c.font())
            cf.setPointSize(7)
            cf.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 115)
            c.setFont(cf)
            lay.addWidget(c)


class LiveCallCard(CardWidget):
    """The in-call hero, rendered as it would look mid-call."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        num = StrongBodyLabel("0035796109901", self)
        nf = QFont("Cascadia Mono")
        nf.setPointSize(15)
        num.setFont(nf)
        top.addWidget(num, 1)
        self.state = InfoBadge.warning("Ringing  ·  00:05", self)
        top.addWidget(self.state)
        lay.addLayout(top)

        via = CaptionLabel("via Teles UK  ·  AAA Tel — C207", self)
        lay.addWidget(via)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        for icon, tip in ((FluentIcon.MICROPHONE, "Mute"),
                          (FluentIcon.PAUSE, "Hold"),
                          (FluentIcon.SHARE, "Transfer")):
            b = ToolButton(icon, self)
            b.setToolTip(tip)
            controls.addWidget(b)
        controls.addStretch(1)
        end = PrimaryPushButton("End call", self)
        end.setStyleSheet(
            "PrimaryPushButton{background:#D84B50;border-color:#D84B50}"
            "PrimaryPushButton:hover{background:#E0575B;border-color:#E0575B}"
        )
        controls.addWidget(end)
        lay.addLayout(controls)


class DialInterface(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dialInterface")
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        supplier = ComboBox(self)
        supplier.addItems(["AAA Tel — C207", "Ibasis (Premium) — C080"])
        header.addWidget(supplier, 1)
        audio = TransparentToolButton(FluentIcon.VOLUME, self)
        header.addWidget(audio)
        root.addLayout(header)

        root.addWidget(LiveCallCard(self))

        dial_row = QHBoxLayout()
        dial_row.setSpacing(8)
        self.number = LineEdit(self)
        self.number.setPlaceholderText("Number or SIP URI")
        self.number.setFont(MONO)
        call = PrimaryPushButton(FluentIcon.PHONE, "Call", self)
        call.setFixedWidth(92)
        dial_row.addWidget(self.number, 1)
        dial_row.addWidget(call)
        root.addLayout(dial_row)

        pad = QGridLayout()
        pad.setSpacing(5)
        for i, (digit, caption) in enumerate(KEYS):
            pad.addWidget(KeyTile(digit, caption, self), i // 3, i % 3)
        root.addLayout(pad)

        head = QHBoxLayout()
        head.addWidget(StrongBodyLabel("Recent calls", self))
        head.addStretch(1)
        head.addWidget(TransparentPushButton("View all", self))
        root.addLayout(head)

        for number, status, level, when in RECENTS:
            row = CardWidget(self)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(12, 6, 10, 6)
            num = BodyLabel(number, row)
            num.setFont(MONO)
            rl.addWidget(num, 1)
            badge = (InfoBadge.success if level == "success"
                     else InfoBadge.error)(status, row)
            rl.addWidget(badge)
            ts = CaptionLabel(when, row)
            rl.addWidget(ts)
            redial = TransparentToolButton(FluentIcon.PHONE, row)
            redial.setToolTip("Redial")
            rl.addWidget(redial)
            root.addWidget(row)

        root.addStretch(1)


class Poc(MSFluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NOC_Beam")
        self.resize(420, 780)
        try:
            self.setMicaEffectEnabled(True)
        except Exception:
            pass
        self.dial = DialInterface(self)
        contacts = QWidget(); contacts.setObjectName("contactsInterface")
        history = QWidget(); history.setObjectName("historyInterface")
        self.addSubInterface(self.dial, FluentIcon.PHONE, "Dial")
        self.addSubInterface(contacts, FluentIcon.PEOPLE, "Contacts")
        self.addSubInterface(history, FluentIcon.HISTORY, "History")
        self.navigationInterface.addItem(
            routeKey="settings", icon=FluentIcon.SETTING, text="Settings",
            onClick=lambda: None, selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )
        try:
            item = self.navigationInterface.widget("historyInterface")
            InfoBadge.attension(
                "3", parent=self.navigationInterface, target=item,
                position=InfoBadgePosition.NAVIGATION_ITEM,
            )
        except Exception:
            pass


def main() -> int:
    app = QApplication(sys.argv)
    setTheme(Theme.DARK)
    setThemeColor("#5B6EE0")
    w = Poc()
    w.show()
    run_loop = getattr(app, "exec")
    return run_loop()


if __name__ == "__main__":
    raise SystemExit(main())
