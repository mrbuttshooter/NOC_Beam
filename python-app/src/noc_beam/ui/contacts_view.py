"""Contacts tab -- Bria-parity layout (search + add + group rows).

Visual structure mirrors Bria's Contacts panel:
  - Search input at top with leading magnifier icon
  - Two action icons on the right: add-group (users) + add-contact (user+plus)
  - Body: list of group rows. Each row has a coloured square avatar
    with the group's first letter, the group name, count on the right,
    and a chevron-down to expand contacts beneath.
  - Expanding a group reveals the contacts inside (Tier J: just labels).

NOC scope: NOC engineers don't typically maintain contacts, but the
tab exists for visual parity with Bria. Default groups are seeded as
quiet placeholders; the panel collapses to the empty state when the
user removes them.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.ui.rail_icons import rail_icon


# Bria seeds three groups: Family / Friends / Work. We keep them as
# parity placeholders -- a NOC tool doesn't really need "Family" but
# matching Bria here was the explicit user ask.
_DEFAULT_GROUPS = (
    ("F", "Family"),
    ("F", "Friends"),
    ("W", "Work"),
)


def _group_avatar(letter: str, color_hex: str = "#E85D04", px: int = 28) -> QPixmap:
    """Square rounded avatar with the group's first letter, Bria-style."""
    pix = QPixmap(QSize(px, px))
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(color_hex))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(0, 0, px, px, 4, 4)
    painter.setPen(QColor("#FFFFFF"))
    f = painter.font()
    f.setPointSize(11)
    f.setBold(True)
    painter.setFont(f)
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, letter.upper())
    painter.end()
    return pix


class GroupRow(QFrame):
    """One Bria-style group row: avatar + name + count + chevron."""

    clicked = Signal(str)

    def __init__(self, letter: str, name: str, count: int = 0,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ContactGroupRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.name = name

        avatar = QLabel(self)
        avatar.setObjectName("GroupAvatar")
        avatar.setPixmap(_group_avatar(letter))
        avatar.setFixedSize(28, 28)

        name_lbl = QLabel(name, self)
        name_lbl.setObjectName("GroupName")

        count_lbl = QLabel(str(count), self)
        count_lbl.setObjectName("GroupCount")

        chev = QToolButton(self)
        chev.setObjectName("GroupChevron")
        chev.setIcon(rail_icon("chevron-down", color="#94A0AD", px=12))
        chev.setIconSize(QSize(12, 12))
        chev.setAutoRaise(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)
        layout.addWidget(avatar)
        layout.addWidget(name_lbl, 1)
        layout.addWidget(count_lbl)
        layout.addWidget(chev)

    def mousePressEvent(self, event):  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.name)
        super().mousePressEvent(event)


class ContactsView(QWidget):
    add_group_requested = Signal()
    add_contact_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ContactsView")

        # ---- Search + actions bar -----------------------------------
        self.search = QLineEdit(self)
        self.search.setObjectName("ContactsSearch")
        self.search.setPlaceholderText("Search Contacts")
        self.search.textChanged.connect(self._apply_filter)

        self.add_group_btn = QToolButton(self)
        self.add_group_btn.setObjectName("ContactsActionBtn")
        self.add_group_btn.setIcon(rail_icon("users", color="#57606A", px=18))
        self.add_group_btn.setIconSize(QSize(18, 18))
        self.add_group_btn.setToolTip("New group")
        self.add_group_btn.clicked.connect(self.add_group_requested.emit)

        self.add_contact_btn = QToolButton(self)
        self.add_contact_btn.setObjectName("ContactsActionBtn")
        self.add_contact_btn.setIcon(rail_icon("user-plus", color="#57606A", px=18))
        self.add_contact_btn.setIconSize(QSize(18, 18))
        self.add_contact_btn.setToolTip("Add contact")
        self.add_contact_btn.clicked.connect(self.add_contact_requested.emit)

        bar = QHBoxLayout()
        bar.setContentsMargins(12, 12, 12, 8)
        bar.setSpacing(6)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.add_group_btn)
        bar.addWidget(self.add_contact_btn)

        # ---- Group rows in a scroll area ----------------------------
        self._rows_holder = QFrame(self)
        self._rows_holder.setObjectName("ContactsBody")
        self._rows_layout = QVBoxLayout(self._rows_holder)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)

        self._rows: list[GroupRow] = []
        for letter, name in _DEFAULT_GROUPS:
            row = GroupRow(letter, name, count=0, parent=self._rows_holder)
            self._rows_layout.addWidget(row)
            self._rows.append(row)
        self._rows_layout.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setObjectName("ContactsScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._rows_holder)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(bar)
        outer.addWidget(scroll, 1)

    def _apply_filter(self, needle: str) -> None:
        n = needle.strip().lower()
        for row in self._rows:
            row.setVisible(not n or n in row.name.lower())
