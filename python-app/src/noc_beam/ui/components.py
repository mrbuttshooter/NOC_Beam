from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.ui.design_tokens import ICON_BUTTON_SIZE


def sip_level(code: int | None) -> str:
    if code is None:
        return "muted"
    if 100 <= code < 200:
        return "progress"
    if 200 <= code < 300:
        return "ok"
    if 300 <= code < 400:
        return "warn"
    if code >= 400:
        return "danger"
    return "muted"


class StatusPill(QLabel):
    def __init__(self, text: str, level: str = "muted", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("StatusPill")
        self.setProperty("level", level)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAccessibleName(f"Status: {text}")


class SipCodeBadge(QLabel):
    def __init__(
        self,
        code: int | None,
        reason: str = "",
        parent: QWidget | None = None,
    ) -> None:
        text = "" if code is None else str(code)
        super().__init__(text, parent)
        self.setObjectName("SipCodeBadge")
        self.setProperty("level", sip_level(code))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if code is not None:
            label = f"{code} {reason}".strip()
            self.setToolTip(label)
            self.setAccessibleName(f"SIP code {label}")


class MetricChip(QLabel):
    def __init__(self, text: str, level: str = "muted", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("MetricChip")
        self.setProperty("level", level)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAccessibleName(text)


class IconActionButton(QToolButton):
    def __init__(self, tooltip: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("IconActionButton")
        self.setFixedSize(ICON_BUTTON_SIZE, ICON_BUTTON_SIZE)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class SectionHeader(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text.upper(), parent)
        self.setObjectName("SectionHeader")


class FormSection(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("FormSection")
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(SectionHeader(title, self))
        layout.addLayout(self.body)


class FooterActionBar(QFrame):
    def __init__(
        self,
        primary_text: str,
        secondary_text: str = "Cancel",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("FooterActionBar")
        self.secondary_button = QPushButton(secondary_text, self)
        self.secondary_button.setObjectName("SecondaryAction")
        self.primary_button = QPushButton(primary_text, self)
        self.primary_button.setObjectName("PrimaryAction")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        layout.addStretch(1)
        layout.addWidget(self.secondary_button)
        layout.addWidget(self.primary_button)


class DenseListRow(QFrame):
    def __init__(
        self,
        title: str,
        subtitle: str = "",
        marker: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("DenseListRow")
        self.marker_label = QLabel(marker, self)
        self.marker_label.setObjectName("DenseRowMarker")
        self.marker_label.setFixedWidth(20)
        self.marker_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("DenseRowTitle")
        self.subtitle_label = QLabel(subtitle, self)
        self.subtitle_label.setObjectName("DenseRowSubtitle")

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        text_col.addWidget(self.title_label)
        text_col.addWidget(self.subtitle_label)

        self.action_holder = QFrame(self)
        self.action_holder.setObjectName("DenseRowActions")
        self.action_layout = QHBoxLayout(self.action_holder)
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(4)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)
        layout.addWidget(self.marker_label)
        layout.addLayout(text_col, 1)
        layout.addWidget(self.action_holder)
