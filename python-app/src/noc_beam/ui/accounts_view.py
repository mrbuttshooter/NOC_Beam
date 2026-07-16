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
from noc_beam.ui.components import StatusPill
from noc_beam.ui.design_tokens import (
    STATUS_DANGER_LIGHT,
    STATUS_MUTED_LIGHT,
    STATUS_OK_LIGHT,
    STATUS_WARN_LIGHT,
)
from noc_beam.ui.rail_icons import rail_icon


# Registration code -> (chip level, dot colour, label). ONE mapping so the
# Accounts list renders registration state exactly like the main-window
# account pill and the Settings registration pill (dot + status chip).
def _registration_status(code: int) -> tuple[str, str, str]:
    if code == 0:
        return "muted", STATUS_MUTED_LIGHT, "Unregistered"
    if 200 <= code < 300:
        return "ok", STATUS_OK_LIGHT, f"Registered ({code})"
    if code in (401, 403, 407):
        return "danger", STATUS_DANGER_LIGHT, f"Auth failed ({code})"
    return "warn", STATUS_WARN_LIGHT, f"Error ({code})"


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
    """A single tight line in the accounts pane (owner feedback 2026-07-16.3:
    "one tight line per account").

      [dot]  Name .................................  [Edit]  [Delete]

    * The SIP URI moved to the row tooltip (was a second mono tier).
    * Registration state is the dot colour + its tooltip (was a separate
      status chip + a last-activity stamp + a badges tier).
    * Edit + Delete are always-visible inline buttons; Test / Unregister /
      Copy URI live in the right-click menu. Double-click = Edit.

    The whole ~36 px row replaces the former 3-tier hover-action card.
    """

    clicked = Signal(str)
    edit_requested = Signal(str)
    test_requested = Signal(str)
    toggle_enabled_requested = Signal(str)
    delete_requested = Signal(str)
    # Unregister lives in the right-click context menu.
    unregister_requested = Signal(str)

    def __init__(self, account: AccountConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AcctRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.account_id = account.id
        self._enabled = account.enabled
        self._last_activity_ts: float | None = None
        self.setProperty("state", "idle")
        self.setFixedHeight(36)

        # ---- Status dot (colour + tooltip carry registration state)
        self.dot = QLabel(self)
        self.dot.setPixmap(_status_dot_pixmap(STATUS_MUTED_LIGHT))
        self.dot.setFixedSize(10, 10)
        self.dot.setToolTip("Unregistered")

        # ---- Name (prefer the UI nickname; URI lives in the tooltip)
        display = (
            getattr(account, "label", "")
            or account.display_name
            or account.username
        )
        self._uri_text = f"sip:{account.username}@{account.domain}"
        self.name = QLabel(display, self)
        self.name.setObjectName("AcctRowName")
        self.name.setToolTip(self._uri_text)
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        # The row itself carries the URI tooltip too, so hovering anywhere
        # (not just the name text) reveals it.
        self.setToolTip(self._uri_text)

        # ---- Always-visible inline actions: Edit + Delete
        def _action_btn(icon_name: str, tooltip: str, signal: Signal) -> QToolButton:
            btn = QToolButton(self)
            btn.setObjectName("AcctRowActionBtn")
            btn.setIcon(rail_icon(icon_name, color="#9BA8B7", px=14))
            btn.setIconSize(QSize(14, 14))
            btn.setToolTip(tooltip)
            btn.setAccessibleName(tooltip)
            btn.setAutoRaise(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, sig=signal: sig.emit(self.account_id))
            return btn

        self.edit_btn = _action_btn("settings", "Edit account", self.edit_requested)
        self.delete_btn = _action_btn("close", "Delete account", self.delete_requested)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 8, 0)
        row.setSpacing(9)
        row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.name, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.edit_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.delete_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    # ------------------------------------------------------------------
    def mousePressEvent(self, event):  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.account_id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802, ANN001
        # Double-click = the primary action, Edit.
        if event.button() == Qt.MouseButton.LeftButton:
            self.edit_requested.emit(self.account_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802, ANN001
        # Quieter actions that don't earn an inline button. popup()
        # (non-blocking) + self-parenting keeps the menu alive after this
        # handler returns.
        from PySide6.QtWidgets import QApplication, QMenu

        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        menu.addAction("Edit…", lambda: self.edit_requested.emit(self.account_id))
        menu.addAction("Test (OPTIONS)", lambda: self.test_requested.emit(self.account_id))
        menu.addAction("Unregister", lambda: self.unregister_requested.emit(self.account_id))
        menu.addSeparator()
        menu.addAction(
            "Copy URI",
            lambda: QApplication.clipboard().setText(self._uri_text),
        )
        menu.addSeparator()
        menu.addAction("Delete…", lambda: self.delete_requested.emit(self.account_id))
        menu.popup(event.globalPos())

    def set_focused(self, focused: bool) -> None:
        self.setProperty("state", "focused" if focused else "idle")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_status(self, code: int) -> None:
        """Update the status dot colour + its tooltip from the single
        _registration_status mapping (same source the main-window account
        pill uses), so the row reads registration state at a glance."""
        level, color, text = _registration_status(code)
        self.dot.setPixmap(_status_dot_pixmap(color, px=10))
        self.dot.setToolTip(text)
        if code != 0:
            self._last_activity_ts = time.time()

    def refresh_relative_time(self) -> None:
        """Kept for AccountsView's 30 s tick (last-activity is now a dot
        tooltip, not a visible label -- nothing to refresh)."""

    def matches_filter(self, needle: str) -> bool:
        if not needle:
            return True
        n = needle.lower()
        return n in self.name.text().lower() or n in self._uri_text.lower()


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
    unregister_requested = Signal(str)   # row context menu (phase 3.1)
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
            row.unregister_requested.connect(self.unregister_requested.emit)
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
