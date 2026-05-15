"""Favorites tab -- Bria-parity layout.

Visual: search input at top + single action icon (manage) + scrollable
list of starred contacts beneath. Empty state when no favorites are
starred yet. Mirrors Bria's Favorites panel one-to-one.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
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


class FavoritesView(QWidget):
    manage_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("FavoritesView")

        self.search = QLineEdit(self)
        self.search.setObjectName("ContactsSearch")
        self.search.setPlaceholderText("Search Favorites")

        self.manage_btn = QToolButton(self)
        self.manage_btn.setObjectName("ContactsActionBtn")
        self.manage_btn.setIcon(rail_icon("user-plus", color="#57606A", px=18))
        self.manage_btn.setIconSize(QSize(18, 18))
        self.manage_btn.setToolTip("Manage favorites")
        self.manage_btn.clicked.connect(self.manage_requested.emit)

        bar = QHBoxLayout()
        bar.setContentsMargins(12, 12, 12, 8)
        bar.setSpacing(6)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.manage_btn)

        # Empty state -- shown until favorites exist.
        self.empty = QLabel(
            "No favorites yet.\n\n"
            "Star a contact in the Contacts tab and it will appear here\n"
            "for one-tap dialing.",
            self,
        )
        self.empty.setObjectName("ViewEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(bar)
        outer.addWidget(self.empty, 1)
