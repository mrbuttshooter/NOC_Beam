"""Accounts master pane (left side of the master/detail layout).

Custom AcctRow widgets instead of a flat QListWidget so we can paint a
status dot, transport badge, and refined typography per row. The whole
master pane sits in a scroll area; selection is communicated via the
selected_account_changed signal.

Detail pane lives in accounts_detail.AccountDetail; MainWindow wires the
selection signal to drive the detail.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.store import AccountConfig


def _status_dot_pixmap(color_hex: str, px: int = 9) -> QPixmap:
    pix = QPixmap(QSize(px, px))
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(color_hex))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(0, 0, px, px)
    painter.end()
    return pix


class AcctRow(QFrame):
    """A single row in the accounts master pane."""

    clicked = Signal(str)  # account_id

    def __init__(self, account: AccountConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AcctRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.account_id = account.id

        # State dynamic property — QSS branches on this. Phase G ships
        # only the focused state; warn/error states wire in Tier 3.
        self.setProperty("state", "idle")

        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)

        self.dot = QLabel(self)
        self.dot.setPixmap(_status_dot_pixmap("#7C8696"))
        self.dot.setFixedSize(9, 9)
        outer.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)

        ident = QVBoxLayout()
        ident.setContentsMargins(0, 0, 0, 0)
        ident.setSpacing(1)
        display = account.display_name or account.username
        self.name = QLabel(display)
        self.name.setObjectName("AcctRowName")
        uri_text = f"{account.username}@{account.domain}"
        self.uri = QLabel(uri_text)
        self.uri.setObjectName("AcctRowUri")
        ident.addWidget(self.name)
        ident.addWidget(self.uri)
        outer.addLayout(ident, 1)

        # Right meta: transport + enabled/disabled
        meta_text = account.transport.upper()
        if not account.enabled:
            meta_text += "  ·  disabled"
        self.meta = QLabel(meta_text)
        self.meta.setObjectName("AcctRowMeta")
        outer.addWidget(self.meta, 0, Qt.AlignmentFlag.AlignVCenter)

    # ------------------------------------------------------------------
    def mousePressEvent(self, event):  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.account_id)
        super().mousePressEvent(event)

    def set_focused(self, focused: bool) -> None:
        # Toggling a dynamic property doesn't auto-restyle; unpolish + polish.
        self.setProperty("state", "focused" if focused else "idle")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_status(self, code: int) -> None:
        """0 = unknown/neutral; 2xx = ok; 4xx auth = warn; other = danger."""
        if code == 0:
            color = "#7C8696"
        elif 200 <= code < 300:
            color = "#66D19E"
        elif code in (401, 403, 407, 423):
            color = "#F0C36D"
        else:
            color = "#FF5C7A"
        self.dot.setPixmap(_status_dot_pixmap(color))

    def matches_filter(self, needle: str) -> bool:
        if not needle:
            return True
        n = needle.lower()
        return n in self.name.text().lower() or n in self.uri.text().lower()


class AccountsView(QWidget):
    """Master pane: count header + Add button + search box + scrollable rows."""

    add_clicked = Signal()
    edit_clicked = Signal()
    remove_clicked = Signal()
    selected_account_changed = Signal(str)  # account_id, "" if cleared

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AcctMaster")
        # Don't pin a fixed width -- the wide MainWindow already gives
        # us 380 px of horizontal real estate, but the narrow PhoneShell
        # is 340 px. Filling whatever is available avoids overflow on
        # the narrow shell and keeps the wide shell looking the same.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._rows: list[AcctRow] = []
        self._selected_id: str | None = None
        self._reg_codes: dict[str, int] = {}

        # ---- Header
        title = QLabel("Accounts")
        title.setObjectName("ViewTitle")
        self.count_label = QLabel("0 of 0 registered")
        self.count_label.setObjectName("ViewCount")
        self.add_btn = QPushButton("+ Add account")
        self.add_btn.setObjectName("PrimaryAction")
        self.add_btn.clicked.connect(self.add_clicked.emit)

        header_top = QHBoxLayout()
        header_top.setContentsMargins(16, 16, 16, 4)
        header_top.setSpacing(8)
        header_top.addWidget(title)
        header_top.addStretch(1)
        header_top.addWidget(self.add_btn)

        count_row = QHBoxLayout()
        count_row.setContentsMargins(16, 0, 16, 12)
        count_row.addWidget(self.count_label)
        count_row.addStretch(1)

        # ---- Search
        self.search = QLineEdit()
        self.search.setObjectName("AcctSearch")
        self.search.setPlaceholderText("Filter (name, URI)")
        self.search.textChanged.connect(self._apply_filter)
        search_wrap = QHBoxLayout()
        search_wrap.setContentsMargins(16, 0, 16, 8)
        search_wrap.addWidget(self.search)

        # ---- Rows in a scroll area
        self._rows_holder = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_holder)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._rows_holder)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Empty-state hint shown when no accounts exist. Lives inside the
        # rows holder so it disappears the moment populate() inserts rows.
        self._empty_label = QLabel(
            "No accounts yet.\n\nClick “+ Add account” above to register\n"
            "your first SIP endpoint.",
            self._rows_holder,
        )
        self._empty_label.setObjectName("ViewEmpty")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._rows_layout.insertWidget(0, self._empty_label)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(header_top)
        outer.addLayout(count_row)
        outer.addLayout(search_wrap)
        outer.addWidget(scroll, 1)

    # ------------------------------------------------------------------
    def populate(self, accounts: list[AccountConfig]) -> None:
        # Tear down any existing rows.
        for row in self._rows:
            row.deleteLater()
        self._rows = []
        # Toggle empty-state visibility based on whether anything's there.
        self._empty_label.setVisible(not accounts)
        # Insert before the trailing stretch (last item).
        insert_at = self._rows_layout.count() - 1
        for cfg in accounts:
            row = AcctRow(cfg, self._rows_holder)
            row.clicked.connect(self._on_row_clicked)
            self._rows_layout.insertWidget(insert_at, row)
            self._rows.append(row)
            insert_at += 1
            # Apply any cached registration code
            code = self._reg_codes.get(cfg.id, 0)
            row.set_status(code)
        self._refresh_count()
        # Re-apply current filter
        self._apply_filter(self.search.text())
        # Try to keep selection; otherwise pick first
        if self._selected_id and any(r.account_id == self._selected_id for r in self._rows):
            self._highlight_selected()
        elif self._rows:
            self._on_row_clicked(self._rows[0].account_id)
        else:
            self._selected_id = None
            self.selected_account_changed.emit("")

    def selected_account_id(self) -> str | None:
        return self._selected_id

    def set_registration_code(self, account_id: str, code: int) -> None:
        self._reg_codes[account_id] = code
        for row in self._rows:
            if row.account_id == account_id:
                row.set_status(code)
                break
        self._refresh_count()

    # ------------------------------------------------------------------
    def _on_row_clicked(self, account_id: str) -> None:
        self._selected_id = account_id
        self._highlight_selected()
        self.selected_account_changed.emit(account_id)

    def _highlight_selected(self) -> None:
        for row in self._rows:
            row.set_focused(row.account_id == self._selected_id)

    def _apply_filter(self, needle: str) -> None:
        for row in self._rows:
            row.setVisible(row.matches_filter(needle))

    def _refresh_count(self) -> None:
        total = len(self._rows)
        registered = sum(
            1 for r in self._rows
            if 200 <= self._reg_codes.get(r.account_id, 0) < 300
        )
        self.count_label.setText(f"{registered} of {total} registered")
