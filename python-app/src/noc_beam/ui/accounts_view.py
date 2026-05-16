"""Accounts master pane -- richer-than-Bria account cards.

Bria shows accounts in a flat table (Enabled / Account Name / Status /
Protocol / User ID / Call / Sync). This view goes wider:

  - Each AcctRow is a 3-tier card: top row carries name + status text
    + last-activity timestamp; middle row shows the SIP URI; bottom row
    is a strip of coloured badges (TLS / SRTP / transport / disabled).
  - Hover reveals per-row action buttons (Edit / Test / Disable /
    Delete) so the operator can manage an account without going through
    a separate dialog.
  - Header gets Add + Refresh All + Test All -- bulk operations Bria
    doesn't expose.
  - Empty state is a designed CTA, not an empty list.

Selection drives the existing AccountDetail right-pane via the
selected_account_changed signal (unchanged contract).
"""
from __future__ import annotations

import time

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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.store import AccountConfig
from noc_beam.ui.rail_icons import rail_icon


def _relative_time(ts: float | None) -> str:
    """'just now' / '2m ago' / '3h ago' / '5d ago' / '-' if None."""
    if ts is None:
        return "-"
    delta = max(0.0, time.time() - ts)
    if delta < 30:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


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


def _badge(text: str, level: str = "neutral") -> QLabel:
    """A small pill label. level in: ok / warn / danger / info / neutral."""
    lbl = QLabel(text.upper())
    lbl.setObjectName("AcctBadge")
    lbl.setProperty("level", level)
    return lbl


class AcctRow(QFrame):
    """A 3-tier card row in the accounts master pane.

    Tier 1 (top row):  status dot + name + status text  ........  last-activity
    Tier 2 (middle):   SIP URI (mono)
    Tier 3 (bottom):   badges (transport, SRTP, auth, disabled)
    Hover overlay:     Edit / Test / Disable / Delete action buttons (right)
    """

    clicked = Signal(str)
    edit_requested = Signal(str)
    test_requested = Signal(str)
    toggle_enabled_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, account: AccountConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AcctRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.account_id = account.id
        self._enabled = account.enabled
        self._last_activity_ts: float | None = None

        self.setProperty("state", "idle")

        # ---- Tier 1: status dot + name + status text + last-activity
        self.dot = QLabel(self)
        self.dot.setPixmap(_status_dot_pixmap("#7C8696"))
        self.dot.setFixedSize(10, 10)

        display = account.display_name or account.username
        self.name = QLabel(display, self)
        self.name.setObjectName("AcctRowName")

        self.status_text = QLabel("Unregistered", self)
        self.status_text.setObjectName("AcctRowStatus")

        self.last_activity = QLabel("never", self)
        self.last_activity.setObjectName("AcctRowMeta")
        self.last_activity.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )

        tier1 = QHBoxLayout()
        tier1.setContentsMargins(0, 0, 0, 0)
        tier1.setSpacing(8)
        tier1.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        tier1.addWidget(self.name, 0)
        tier1.addWidget(self.status_text, 1)
        tier1.addWidget(self.last_activity, 0)

        # ---- Tier 2: URI mono
        uri_text = f"sip:{account.username}@{account.domain}"
        self.uri = QLabel(uri_text, self)
        self.uri.setObjectName("AcctRowUri")
        # Do NOT enable TextSelectableByMouse here -- on Windows it
        # makes Qt render the QLabel like a QLineEdit (white inset
        # box, flat border) which was the "white text field" complaint.
        # Right-click → Copy URI on the kebab still works for selection.
        self.uri.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # ---- Tier 3: badges row
        self.badges_layout = QHBoxLayout()
        self.badges_layout.setContentsMargins(0, 0, 0, 0)
        self.badges_layout.setSpacing(4)
        # Transport badge
        transport_level = "info" if account.transport == "tls" else "neutral"
        self.badges_layout.addWidget(_badge(account.transport, transport_level))
        # SRTP badge
        if account.srtp != "disabled":
            self.badges_layout.addWidget(_badge(f"SRTP {account.srtp}", "ok"))
        # Auth-different badge
        if account.auth_user and account.auth_user != account.username:
            self.badges_layout.addWidget(_badge(f"auth {account.auth_user}", "neutral"))
        # Disabled badge
        if not account.enabled:
            self.badges_layout.addWidget(_badge("disabled", "warn"))
        self.badges_layout.addStretch(1)

        # ---- Hover-revealed action buttons row (overlay on the right)
        self.actions = QFrame(self)
        self.actions.setObjectName("AcctRowActions")
        self.actions.setVisible(False)  # only on hover
        # Reserve the action row's height even when hidden so the row
        # geometry doesn't jump +28px on hover (which used to shove
        # every row below downward, creating the layout jitter the
        # round-4/5/6 audits flagged). retainSizeWhenHidden keeps the
        # layout space allocated.
        sp = self.actions.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.actions.setSizePolicy(sp)
        ar = QHBoxLayout(self.actions)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.setSpacing(2)
        for icon_name, tooltip, signal in (
            ("settings",   "Edit account",    self.edit_requested),
            ("trace",      "Test (OPTIONS)",  self.test_requested),
            ("close",      "Delete account",  self.delete_requested),
        ):
            btn = QToolButton(self.actions)
            btn.setObjectName("AcctRowActionBtn")
            # Use a mid-grey that works on BOTH light (~#FFFFFF bg) and
            # dark (~#161B22 bg). #57606A was almost invisible on dark.
            # Tooltip + accessibleName carry semantic intent so colour
            # is decorative; #9BA8B7 has ~4.5:1 contrast on both
            # canvases per a quick eyeball.
            btn.setIcon(rail_icon(icon_name, color="#9BA8B7", px=14))
            btn.setIconSize(QSize(14, 14))
            btn.setToolTip(tooltip)
            btn.setAccessibleName(tooltip)
            btn.setAutoRaise(True)
            btn.clicked.connect(lambda _=False, sig=signal: sig.emit(self.account_id))
            ar.addWidget(btn)

        # Compose tiers in a vertical stack
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(4)
        outer.addLayout(tier1)
        outer.addWidget(self.uri)
        outer.addLayout(self.badges_layout)
        # Action overlay: dock in bottom-right corner of the row via a
        # secondary QHBoxLayout that gets stretched to push actions right
        outer.addWidget(self.actions, 0, Qt.AlignmentFlag.AlignRight)

    # ------------------------------------------------------------------
    def mousePressEvent(self, event):  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.account_id)
        super().mousePressEvent(event)

    def enterEvent(self, event):  # noqa: N802, ANN001
        self.actions.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802, ANN001
        self.actions.setVisible(False)
        super().leaveEvent(event)

    def set_focused(self, focused: bool) -> None:
        self.setProperty("state", "focused" if focused else "idle")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_status(self, code: int) -> None:
        """Update status dot colour + status text + last-activity stamp."""
        if code == 0:
            color, text = "#7C8696", "Unregistered"
        elif 200 <= code < 300:
            color, text = "#66D19E", f"Registered ({code})"
        elif code in (401, 403, 407, 423):
            color, text = "#F0C36D", f"Auth failed ({code})"
        elif code == 408:
            color, text = "#FF5C7A", f"Timeout ({code})"
        else:
            color, text = "#FF5C7A", f"Error ({code})"
        self.dot.setPixmap(_status_dot_pixmap(color, px=10))
        self.status_text.setText(text)
        if code != 0:
            self._last_activity_ts = time.time()
            self.last_activity.setText(_relative_time(self._last_activity_ts))

    def refresh_relative_time(self) -> None:
        """Called by AccountsView's refresh tick to keep the timestamp fresh."""
        self.last_activity.setText(_relative_time(self._last_activity_ts))

    def matches_filter(self, needle: str) -> bool:
        if not needle:
            return True
        n = needle.lower()
        return n in self.name.text().lower() or n in self.uri.text().lower()


class AccountsView(QWidget):
    """Accounts surface: header + bulk actions + search + rich rows.

    Bulk actions (Refresh All / Test All) operate on every enabled
    account at once -- something Bria's flat table can't do. Per-row
    actions (Edit / Test / Delete) are revealed on hover, exposed via
    the new edit_requested / test_requested / delete_requested signals
    keyed by account_id.
    """

    # Legacy "use selected account" signals, kept for backwards-compat.
    add_clicked = Signal()
    edit_clicked = Signal()
    remove_clicked = Signal()
    selected_account_changed = Signal(str)  # account_id, "" if cleared
    # New per-row signals (carry the account_id directly).
    edit_requested = Signal(str)
    test_requested = Signal(str)
    delete_requested = Signal(str)
    refresh_all_requested = Signal()
    test_all_requested = Signal()

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

        # ---- Header (title + count) + bulk-action toolbar
        title = QLabel("Accounts")
        title.setObjectName("ViewTitle")
        self.count_label = QLabel("0 of 0 registered")
        self.count_label.setObjectName("ViewCount")
        self.add_btn = QPushButton("+ Add account")
        self.add_btn.setObjectName("PrimaryAction")
        self.add_btn.clicked.connect(self.add_clicked.emit)

        # Bulk actions: "Refresh all" re-issues REGISTER on every enabled
        # account; "Test all" runs an OPTIONS probe on each. Bria has
        # neither -- they're our differentiator.
        self.refresh_all_btn = QPushButton("Refresh all")
        self.refresh_all_btn.setObjectName("AcctBulkBtn")
        self.refresh_all_btn.clicked.connect(self.refresh_all_requested.emit)
        self.test_all_btn = QPushButton("Test all")
        self.test_all_btn.setObjectName("AcctBulkBtn")
        self.test_all_btn.clicked.connect(self.test_all_requested.emit)

        header_top = QHBoxLayout()
        header_top.setContentsMargins(16, 16, 16, 4)
        header_top.setSpacing(8)
        header_top.addWidget(title)
        header_top.addStretch(1)
        header_top.addWidget(self.add_btn)

        count_row = QHBoxLayout()
        count_row.setContentsMargins(16, 0, 16, 8)
        count_row.setSpacing(8)
        count_row.addWidget(self.count_label)
        count_row.addStretch(1)
        count_row.addWidget(self.refresh_all_btn)
        count_row.addWidget(self.test_all_btn)

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

        # Tick the relative-time labels every 30 s so "2m ago" doesn't
        # silently drift to wrong values.
        from PySide6.QtCore import QTimer

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(30_000)
        self._tick_timer.timeout.connect(self._tick_relative_times)
        self._tick_timer.start()

    # ------------------------------------------------------------------
    def populate(self, accounts: list[AccountConfig]) -> None:
        # Tear down any existing rows.
        for row in self._rows:
            row.deleteLater()
        self._rows = []
        # Toggle empty-state visibility based on whether anything's there.
        self._empty_label.setVisible(not accounts)
        # Bulk action buttons make no sense when there are no accounts.
        for b in (self.refresh_all_btn, self.test_all_btn):
            b.setEnabled(bool(accounts))
        # Insert before the trailing stretch (last item).
        insert_at = self._rows_layout.count() - 1
        for cfg in accounts:
            row = AcctRow(cfg, self._rows_holder)
            row.clicked.connect(self._on_row_clicked)
            row.edit_requested.connect(self.edit_requested.emit)
            row.test_requested.connect(self.test_requested.emit)
            row.delete_requested.connect(self.delete_requested.emit)
            self._rows_layout.insertWidget(insert_at, row)
            self._rows.append(row)
            insert_at += 1
            # Apply any cached registration code
            code = self._reg_codes.get(cfg.id, 0)
            row.set_status(code)
        self._refresh_count()
        # Re-apply current search filter against the rebuilt rows.
        # Previously a stale needle in self.search was ignored after
        # a populate() rebuild -- the user saw the unfiltered list
        # until they touched the search box.
        try:
            self._apply_filter(self.search.text())
        except Exception:
            pass

    def _tick_relative_times(self) -> None:
        # 30-second tick refreshes "registered 2m ago" relative
        # timestamps in place. Previously it also re-applied the
        # filter AND auto-reselected the first row when selection
        # was cleared -- which silently re-emitted
        # selected_account_changed every half-minute even when the
        # user had deliberately cleared selection. Just refresh
        # the relative-time labels; nothing else.
        for row in self._rows:
            row.refresh_relative_time()

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
