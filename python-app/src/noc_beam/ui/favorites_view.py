"""Favorites tab backed by persisted starred contacts."""
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

from noc_beam.config.contacts import Contact, load_contacts
from noc_beam.ui.rail_icons import rail_icon


class FavoriteRow(QFrame):
    call_requested = Signal(str)

    def __init__(self, contact: Contact, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DenseListRow")
        self.contact = contact
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        marker = QLabel("*", self)
        marker.setObjectName("DenseRowMarker")
        marker.setFixedWidth(20)
        marker.setAlignment(Qt.AlignmentFlag.AlignCenter)

        name_lbl = QLabel(contact.name, self)
        name_lbl.setObjectName("DenseRowTitle")
        number_lbl = QLabel(contact.number, self)
        number_lbl.setObjectName("DenseRowSubtitle")

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        text_col.addWidget(name_lbl)
        text_col.addWidget(number_lbl)

        call_btn = QToolButton(self)
        call_btn.setObjectName("IconActionButton")
        call_btn.setIcon(rail_icon("calls", color="#2DA44E", px=16))
        call_btn.setIconSize(QSize(16, 16))
        call_btn.setToolTip("Call")
        call_btn.clicked.connect(lambda: self.call_requested.emit(self.contact.number))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 7, 8, 7)
        layout.setSpacing(8)
        layout.addWidget(marker)
        layout.addLayout(text_col, 1)
        layout.addWidget(call_btn)

    def mouseDoubleClickEvent(self, event):  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.call_requested.emit(self.contact.number)
        super().mouseDoubleClickEvent(event)


class FavoritesView(QWidget):
    manage_requested = Signal()
    call_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("FavoritesView")
        self._contacts: list[Contact] = []

        self.search = QLineEdit(self)
        self.search.setObjectName("ContactsSearch")
        self.search.setPlaceholderText("Search Favorites")
        self.search.textChanged.connect(self._render)

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

        self._rows_holder = QFrame(self)
        self._rows_holder.setObjectName("ContactsBody")
        self._rows_layout = QVBoxLayout(self._rows_holder)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)

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

        self.reload()

    def reload(self) -> None:
        self._contacts = [contact for contact in load_contacts() if contact.favorite]
        self._render()

    def _render(self) -> None:
        self._clear_rows()
        needle = self.search.text().strip().lower()
        contacts = [contact for contact in self._contacts if self._matches(contact, needle)]

        if not contacts:
            text = "No favorites yet." if not needle else "No favorites match your search."
            empty = QLabel(text, self._rows_holder)
            empty.setObjectName("ViewEmpty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            self._rows_layout.addWidget(empty, 1)
            return

        for contact in sorted(contacts, key=lambda item: item.name.lower()):
            row = FavoriteRow(contact, self._rows_holder)
            row.call_requested.connect(self.call_requested.emit)
            self._rows_layout.addWidget(row)
        self._rows_layout.addStretch(1)

    def _clear_rows(self) -> None:
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _matches(self, contact: Contact, needle: str) -> bool:
        if not needle:
            return True
        haystack = f"{contact.name} {contact.number} {contact.group}".lower()
        return needle in haystack
