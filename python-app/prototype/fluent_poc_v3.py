"""NOC_Beam fluent-clean POC v3 -- full-app tour.

FluentWindow (expandable labeled sidebar) shell with four demo pages:
Dial (compact), Accounts, Test runner, Settings -- showing what every
major NOC_Beam surface becomes in the fluent language. Pure look
artifact, no SIP wiring.

Run:  .venv\\Scripts\\python.exe prototype\\fluent_poc_v3.py [page]
      page: dial | accounts | runner | settings (start page)
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    ComboBoxSettingCard,
    FluentIcon,
    InfoBadge,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    FluentWindow,
    NavigationItemPosition,
    OptionsConfigItem,
    OptionsValidator,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    QConfig,
    SegmentedWidget,
    StrongBodyLabel,
    SwitchButton,
    SwitchSettingCard,
    TableWidget,
    Theme,
    TransparentPushButton,
    TransparentToolButton,
    setTheme,
    setThemeColor,
)

MONO = QFont("Cascadia Mono")
MONO.setPointSize(10)


class _Cfg(QConfig):
    theme_mode = OptionsConfigItem(
        "Appearance", "Theme", "Dark",
        OptionsValidator(["Dark", "Light", "Follow system"]),
    )
    dtmf = OptionsConfigItem(
        "Calls", "Dtmf", "rfc2833",
        OptionsValidator(["rfc2833", "SIP INFO", "in-band"]),
    )


CFG = _Cfg()


class DialPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dialPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(10)
        row = QHBoxLayout()
        num = LineEdit(self)
        num.setPlaceholderText("Number or SIP URI")
        num.setFont(MONO)
        call = PrimaryPushButton(FluentIcon.PHONE, "Call", self)
        row.addWidget(num, 1)
        row.addWidget(call)
        root.addLayout(row)
        root.addWidget(CaptionLabel(
            "(compact dial page — full version shown in POC v2)", self))
        root.addStretch(1)


class AccountsPage(QWidget):
    ACCOUNTS = [
        ("Teles UK", "sip:U207@208.87.170.99", "UDP", "Registered", "success", True),
        ("Teles NY", "sip:N207@208.87.169.100", "TCP", "Unregistered", "muted", True),
        ("Genband NY", "sip:96175624180@208.87.169.120", "TCP", "Auth failed (403)", "error", True),
        ("Genband UK", "sip:96678549025@208.87.170.120", "TCP", "Unregistered", "muted", False),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("accountsPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(StrongBodyLabel("SIP accounts", self))
        head.addStretch(1)
        head.addWidget(PushButton(FluentIcon.SYNC, "Test all", self))
        head.addWidget(PrimaryPushButton(FluentIcon.ADD, "Add account", self))
        root.addLayout(head)
        for name, uri, transport, status, level, enabled in self.ACCOUNTS:
            card = CardWidget(self)
            cl = QHBoxLayout(card)
            cl.setContentsMargins(16, 10, 12, 10)
            col = QVBoxLayout()
            col.setSpacing(2)
            col.addWidget(StrongBodyLabel(name, card))
            sub = CaptionLabel(f"{uri}  ·  {transport}", card)
            sub.setFont(MONO)
            col.addWidget(sub)
            cl.addLayout(col, 1)
            badge = {"success": InfoBadge.success, "error": InfoBadge.error,
                     "muted": InfoBadge.info}[level](status, card)
            cl.addWidget(badge)
            sw = SwitchButton(card)
            sw.setChecked(enabled)
            cl.addWidget(sw)
            cl.addWidget(TransparentToolButton(FluentIcon.EDIT, card))
            root.addWidget(card)
        root.addStretch(1)


class RunnerPage(QWidget):
    ROWS = [
        ("1", "Teles UK", "sip:00212626154358@208.87.170.99", "FAIL", "503", "233 ms"),
        ("2", "Teles UK", "sip:00212626748875@208.87.170.99", "FAIL", "503", "63 ms"),
        ("3", "Teles UK", "sip:0035796109901@208.87.170.99", "PASS", "180", "104 ms"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("runnerPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(10)
        seg = SegmentedWidget(self)
        for key, label in (("cfg", "Configure"), ("run", "Running 2/3"),
                           ("res", "Results")):
            seg.addItem(key, label)
        seg.setCurrentItem("run")
        root.addWidget(seg)
        bar = ProgressBar(self)
        bar.setValue(66)
        root.addWidget(bar)
        badges = QHBoxLayout()
        badges.addWidget(InfoBadge.success("1 passed", self))
        badges.addWidget(InfoBadge.error("2 failed", self))
        badges.addWidget(InfoBadge.info("1 running", self))
        badges.addStretch(1)
        badges.addWidget(PushButton(FluentIcon.DOWNLOAD, "Export", self))
        badges.addWidget(PrimaryPushButton(FluentIcon.PLAY, "Run 3 calls", self))
        root.addLayout(badges)
        table = TableWidget(self)
        table.setColumnCount(6)
        table.setRowCount(len(self.ROWS))
        table.setHorizontalHeaderLabels(["#", "From", "To", "Result", "Code", "RTT"])
        for r, row in enumerate(self.ROWS):
            for c, val in enumerate(row):
                item = QTableWidgetItem(val)
                if c in (2, 4, 5):
                    item.setFont(MONO)
                table.setItem(r, c, item)
        table.resizeColumnsToContents()
        root.addWidget(table, 1)


class SettingsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(8)
        root.addWidget(StrongBodyLabel("Appearance", self))
        root.addWidget(ComboBoxSettingCard(
            CFG.theme_mode, FluentIcon.BRUSH, "Theme",
            "Dark by default; light for bright NOC floors",
            texts=["Dark", "Light", "Follow system"], parent=self))
        root.addWidget(StrongBodyLabel("Calls", self))
        root.addWidget(ComboBoxSettingCard(
            CFG.dtmf, FluentIcon.COMMAND_PROMPT, "DTMF mode",
            "How mid-call digits are signalled",
            texts=["rfc2833", "SIP INFO", "in-band"], parent=self))
        root.addStretch(1)


class Poc(FluentWindow):
    def __init__(self, start: str = "dial"):
        super().__init__()
        self.setWindowTitle("NOC_Beam")
        self.resize(760, 640)
        try:
            self.setMicaEffectEnabled(True)
        except Exception:
            pass
        pages = {
            "dial": (DialPage(self), FluentIcon.PHONE, "Dial"),
            "accounts": (AccountsPage(self), FluentIcon.PEOPLE, "Accounts"),
            "runner": (RunnerPage(self), FluentIcon.SPEED_HIGH, "Test runner"),
        }
        for key, (page, icon, label) in pages.items():
            self.addSubInterface(page, icon, label)
        settings = SettingsPage(self)
        self.addSubInterface(
            settings, FluentIcon.SETTING, "Settings",
            position=NavigationItemPosition.BOTTOM,
        )
        pages["settings"] = (settings, None, None)
        self._start_target = pages.get(start, pages["dial"])[0]
        if start == "accounts":
            InfoBar.success(
                "Registered", "Teles UK is registered (200 OK).",
                position=InfoBarPosition.BOTTOM_RIGHT, duration=-1, parent=self,
            )


def main() -> int:
    start = sys.argv[1] if len(sys.argv) > 1 else "dial"
    app = QApplication(sys.argv)
    setTheme(Theme.DARK)
    setThemeColor("#5B6EE0")
    w = Poc(start)
    w.show()
    from PySide6.QtCore import QTimer
    QTimer.singleShot(400, lambda: w.switchTo(w._start_target))
    run_loop = getattr(app, "exec")
    return run_loop()


if __name__ == "__main__":
    raise SystemExit(main())
