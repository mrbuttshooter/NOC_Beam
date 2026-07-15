"""NOC_Beam fluent-clean proof of concept.

Standalone visual prototype of the Dial surface built on qfluentwidgets
(MSFluentWindow shell + fluent components). NO SIP wiring -- this exists
so the owner can approve the look from a real rendered window before we
port the app onto it.

Run:  .venv\\Scripts\\python.exe prototype\\fluent_poc.py
"""
from __future__ import annotations

import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    InfoBadge,
    LineEdit,
    MSFluentWindow,
    NavigationItemPosition,
    PrimaryPushButton,
    StrongBodyLabel,
    Theme,
    TransparentPushButton,
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
    ("35796109901", "Cancelled", "error", "10:47"),
    ("96899406599", "Not found", "error", "11:48"),
    ("96170010600", "Answered", "success", "17:06"),
    ("96170010800", "Cancelled", "error", "17:03"),
]


class DialInterface(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dialInterface")
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        supplier = ComboBox(self)
        supplier.addItems(["AAA Tel — C207", "Ibasis (Premium) — C080"])
        root.addWidget(supplier)

        dial_row = QHBoxLayout()
        dial_row.setSpacing(8)
        self.number = LineEdit(self)
        self.number.setPlaceholderText("Number or SIP URI")
        mono = QFont("Cascadia Mono")
        mono.setPointSize(11)
        self.number.setFont(mono)
        call = PrimaryPushButton(FluentIcon.PHONE, "Call", self)
        call.setFixedWidth(96)
        dial_row.addWidget(self.number, 1)
        dial_row.addWidget(call)
        root.addLayout(dial_row)

        pad = QGridLayout()
        pad.setSpacing(4)
        for i, (digit, caption) in enumerate(KEYS):
            b = TransparentPushButton(digit, self)
            b.setFixedHeight(36)
            pad.addWidget(b, i // 3, i % 3)
        root.addLayout(pad)

        head = QHBoxLayout()
        head.addWidget(StrongBodyLabel("Recent calls", self))
        head.addStretch(1)
        view_all = TransparentPushButton("View all", self)
        head.addWidget(view_all)
        root.addLayout(head)

        for number, status, level, when in RECENTS:
            row = QWidget(self)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(4, 2, 4, 2)
            num = BodyLabel(number, row)
            num.setFont(mono)
            rl.addWidget(num, 1)
            badge = (InfoBadge.success if level == "success"
                     else InfoBadge.error)(status, row)
            rl.addWidget(badge)
            ts = CaptionLabel(when, row)
            rl.addWidget(ts)
            root.addWidget(row)

        root.addStretch(1)


class Poc(MSFluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NOC_Beam")
        self.resize(400, 620)
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
