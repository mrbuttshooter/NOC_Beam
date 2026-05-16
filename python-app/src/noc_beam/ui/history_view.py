"""Call history as a Bria-style row list (not a table).

Each row shows the peer URI prominently with a small meta line
underneath. Double-click redials the peer (Bria parity); the per-row
info button or right-click context menu opens CdrDetailDialog with
every field plus Export CSV.
"""
from __future__ import annotations

import time

from datetime import datetime, timedelta

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.history import (
    CdrEntry,
    clear_history,
    load_history,
    load_last_seen_ended_at,
    save_last_seen_ended_at,
)
from noc_beam.ui.cdr_detail_dialog import CdrDetailDialog
from noc_beam.ui.components import SipCodeBadge


def _bucket_label(ts: float) -> str:
    """Human bucket name for a timestamp."""
    if ts <= 0:
        return "Earlier"
    when = datetime.fromtimestamp(ts).date()
    today = datetime.now().date()
    if when == today:
        return "Today"
    if when == today - timedelta(days=1):
        return "Yesterday"
    if today - when < timedelta(days=7):
        return when.strftime("%A")
    return when.strftime("%b %d, %Y")


class _DateDivider(QLabel):
    """Section header label between groups of CDR rows on different dates."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("HistoryDivider")
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def _fmt_when(ts: float) -> str:
    now = time.time()
    if ts <= 0:
        return "-"
    same_day = time.strftime("%Y-%m-%d", time.localtime(now)) == \
               time.strftime("%Y-%m-%d", time.localtime(ts))
    if same_day:
        return time.strftime("%H:%M", time.localtime(ts))
    return time.strftime("%m/%d %H:%M", time.localtime(ts))


def _fmt_duration(seconds: float) -> str:
    if seconds <= 0:
        return ""
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _arrow(entry: CdrEntry) -> str:
    """Single-glyph direction marker."""
    if entry.direction == "in":
        return "▼" if entry.was_answered else "✕"   # in / missed
    return "▲" if entry.was_answered else "✕"       # out / failed


def _result_class(entry: CdrEntry) -> str:
    if entry.direction == "in" and not entry.was_answered:
        return "missed"
    if entry.direction == "out" and not entry.was_answered:
        return "failed"
    return "ok"


class HistoryRow(QFrame):
    """One CDR row.

    - Double-click redials the peer (Bria parity -- the user explicitly
      asked for this).
    - The (i) button opens CdrDetailDialog.
    - The phone button redials.
    - Right-click opens context menu (Redial / Detail / Copy URI / Delete).
    """

    activated = Signal(int)            # entry index in the parent's list
    redial = Signal(str)               # peer_uri
    delete_requested = Signal(int)
    copy_requested = Signal(str)

    def __init__(self, entry: CdrEntry, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._index = index
        self._entry = entry
        self.setObjectName("HistoryRow")
        self.setProperty("result", _result_class(entry))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        arrow_lbl = QLabel(_arrow(entry))
        arrow_lbl.setObjectName("HistoryRowArrow")
        arrow_lbl.setProperty("result", _result_class(entry))
        arrow_lbl.setFixedWidth(20)
        arrow_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Peer + meta labels get Ignored horizontal size policy so the
        # text column can shrink when the window is narrow. Without
        # this, the labels enforce their full text-width as a hard
        # minimum and the badge / info / call buttons get clipped off
        # the right edge (same root cause as the call-card overflow
        # fixed in 11c7ca6). The tooltip carries the FULL URI so the
        # @domain is one hover away when needed.
        from PySide6.QtWidgets import QSizePolicy as _SP
        from noc_beam.ui.quick_dial import _short_uri as _strip_uri
        peer_full = entry.peer_uri or "(unknown)"
        peer_display = _strip_uri(peer_full) or peer_full
        peer_lbl = QLabel(peer_display)
        peer_lbl.setObjectName("HistoryRowPeer")
        peer_lbl.setProperty("result", _result_class(entry))
        peer_lbl.setToolTip(peer_full)
        peer_lbl.setSizePolicy(_SP.Policy.Ignored, _SP.Policy.Preferred)

        when = _fmt_when(entry.ended_at or entry.started_at)
        dur = _fmt_duration(entry.duration_s)
        bits = [when]
        if dur:
            bits.append(dur)
        if entry.end_code and not entry.was_answered:
            bits.append(f"{entry.end_code} {entry.end_reason}".strip())
        meta_lbl = QLabel(" · ".join(b for b in bits if b))
        meta_lbl.setObjectName("HistoryRowMeta")
        meta_lbl.setSizePolicy(_SP.Policy.Ignored, _SP.Policy.Preferred)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        text_col.addWidget(peer_lbl)
        text_col.addWidget(meta_lbl)

        self._info_btn = QToolButton(self)
        self._info_btn.setObjectName("HistoryRowInfo")
        self._info_btn.setText("i")
        self._info_btn.setToolTip("Show full call detail (codec, duration, end code)")
        self._info_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._info_btn.clicked.connect(lambda: self.activated.emit(self._index))

        self._call_btn = QToolButton(self)
        self._call_btn.setObjectName("HistoryRowCall")
        self._call_btn.setText("\U0001F4DE")
        self._call_btn.setToolTip(
            f"Call {entry.peer_uri}" if entry.peer_uri else "No peer URI to call back"
        )
        self._call_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._call_btn.setEnabled(bool(entry.peer_uri))
        self._call_btn.clicked.connect(self._emit_redial)

        code = entry.end_code if entry.end_code else (200 if entry.was_answered else None)
        badge = SipCodeBadge(code, entry.end_reason, self)

        outer = QHBoxLayout(self)
        # Tighter margins + spacing so the row fits in the compact
        # softphone width even with the badge + info + call buttons
        # all visible. The text column shrinks to fill what's left.
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)
        outer.addWidget(arrow_lbl, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(text_col, 1)
        outer.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self._info_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self._call_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _emit_redial(self) -> None:
        if self._entry.peer_uri:
            self.redial.emit(self._entry.peer_uri)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802, ANN001
        # Bria parity: double-click redials. Don't redial if the click
        # landed on a child button (info/call) -- those have their own
        # single-click actions.
        ch = self.childAt(event.pos())
        if ch in (self._call_btn, self._info_btn):
            event.accept()
            return
        self._emit_redial()
        event.accept()

    def _show_context_menu(self, pos) -> None:
        menu = QMenu(self)
        if self._entry.peer_uri:
            act_call = QAction(f"Call {self._entry.peer_uri}", menu)
            act_call.triggered.connect(self._emit_redial)
            menu.addAction(act_call)
        act_detail = QAction("Show full detail…", menu)
        act_detail.triggered.connect(lambda: self.activated.emit(self._index))
        menu.addAction(act_detail)
        if self._entry.peer_uri:
            act_copy = QAction("Copy peer URI", menu)
            act_copy.triggered.connect(
                lambda: self.copy_requested.emit(self._entry.peer_uri)
            )
            menu.addAction(act_copy)
        menu.addSeparator()
        act_del = QAction("Delete entry", menu)
        act_del.triggered.connect(lambda: self.delete_requested.emit(self._index))
        menu.addAction(act_del)
        menu.popup(self.mapToGlobal(pos))


class HistoryView(QWidget):
    """List of HistoryRow widgets backed by the on-disk CDR store."""

    redial_requested = Signal(str)
    missed_count_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[CdrEntry] = []
        self._rows: list[HistoryRow] = []
        # Persisted across restarts so the badge doesn't re-light every
        # prior missed call on next launch.
        self._last_seen_ended_at: float = load_last_seen_ended_at()

        self._search = QLineEdit()
        self._search.setObjectName("HistorySearch")
        self._search.setAccessibleName("History search")
        self._search.setPlaceholderText("Search peer URI / number…")
        self._search.setClearButtonEnabled(True)
        # 150ms debounce: _refresh_rows tears down + rebuilds every row +
        # date divider, which on a 1000-CDR history was stalling the UI
        # mid-keystroke. Coalesce a burst of typing into one rebuild.
        from PySide6.QtCore import QTimer as _QT
        self._search_debounce = _QT(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self._refresh_rows)
        self._search.textChanged.connect(
            lambda _t: self._search_debounce.start()
        )
        # The clear-button QLineEdit creates internally is a QToolButton
        # with no text/icon/accessibleName -- which breaks the a11y
        # contract test that walks every QPushButton+QToolButton in the
        # shell. Tag it with a tooltip + accessible name so it counts.
        from PySide6.QtWidgets import QToolButton as _QTB
        for tb in self._search.findChildren(_QTB):
            if not tb.accessibleName():
                tb.setAccessibleName("Clear search")
                tb.setToolTip("Clear search")

        self._dir_filter = QComboBox()
        self._dir_filter.setObjectName("HistoryFilter")
        self._dir_filter.addItem("All Calls", "all")
        self._dir_filter.addItem("Incoming", "in")
        self._dir_filter.addItem("Outgoing", "out")
        self._dir_filter.addItem("Missed", "missed")
        self._dir_filter.currentIndexChanged.connect(self._refresh_rows)

        self._range_filter = QComboBox()
        self._range_filter.setObjectName("HistoryFilter")
        self._range_filter.addItem("All time", "all")
        self._range_filter.addItem("Today", "today")
        self._range_filter.addItem("Yesterday", "yesterday")
        self._range_filter.addItem("Last 7 days", "week")
        self._range_filter.addItem("Last 30 days", "month")
        self._range_filter.currentIndexChanged.connect(self._refresh_rows)

        self._reload_btn = QToolButton()
        self._reload_btn.setObjectName("HistoryIconBtn")
        self._reload_btn.setText("⟳")
        self._reload_btn.setToolTip("Reload from disk")
        self._reload_btn.clicked.connect(self.reload)
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setObjectName("HistoryClearBtn")
        self._clear_btn.clicked.connect(self._on_clear)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(8, 6, 8, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._search, 1)

        controls = QHBoxLayout()
        controls.setContentsMargins(8, 4, 8, 4)
        controls.setSpacing(6)
        controls.addWidget(self._dir_filter)
        controls.addWidget(self._range_filter)
        controls.addStretch(1)
        controls.addWidget(self._reload_btn)
        controls.addWidget(self._clear_btn)

        self._empty_label = QLabel(
            "No call history yet.\n\n"
            "Placed and received calls will appear here.\n"
            "Double-click a row to call back, or use the (i) button for full detail."
        )
        self._empty_label.setObjectName("ViewEmpty")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)

        self._rows_holder = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_holder)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch(1)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("HistoryScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setWidget(self._rows_holder)

        self._stack = QStackedLayout()
        self._stack.addWidget(self._empty_label)
        self._stack.addWidget(self._scroll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(search_row)
        layout.addLayout(controls)
        layout.addLayout(self._stack, 1)

        self.reload()

    def mark_all_seen(self) -> None:
        """Reset the unread-missed counter AND persist the new
        high-water-mark so the badge doesn't re-light every prior
        missed call after restart."""
        if self._entries:
            self._last_seen_ended_at = max(e.ended_at for e in self._entries)
            try:
                save_last_seen_ended_at(self._last_seen_ended_at)
            except Exception:
                pass
        self.missed_count_changed.emit(0)

    def unread_missed_count(self) -> int:
        return sum(
            1 for e in self._entries
            if e.direction == "in" and not e.was_answered
            and e.ended_at > self._last_seen_ended_at
        )

    def reload(self) -> None:
        self._entries = sorted(load_history(), key=lambda e: e.ended_at, reverse=True)
        self._refresh_rows()
        self.missed_count_changed.emit(self.unread_missed_count())

    def _refresh_rows(self) -> None:
        # Tear down everything (rows AND date dividers).
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                try:
                    w.blockSignals(True)
                except Exception:
                    pass
                w.deleteLater()
        self._rows.clear()

        visible = [e for e in self._entries if self._matches_filters(e)]
        if not visible:
            self._stack.setCurrentIndex(0)
            return

        self._stack.setCurrentIndex(1)
        last_bucket: str | None = None
        insert_at = 0
        for i, entry in enumerate(visible):
            bucket = _bucket_label(entry.ended_at or entry.started_at)
            if bucket != last_bucket:
                divider = _DateDivider(bucket, self._rows_holder)
                self._rows_layout.insertWidget(insert_at, divider)
                insert_at += 1
                last_bucket = bucket
            row = HistoryRow(entry, i, self._rows_holder)
            row.activated.connect(self._open_detail)
            row.redial.connect(self.redial_requested.emit)
            row.delete_requested.connect(self._on_delete_one)
            row.copy_requested.connect(self._on_copy_uri)
            self._rows_layout.insertWidget(insert_at, row)
            insert_at += 1
            self._rows.append(row)

    def _matches_filters(self, entry: CdrEntry) -> bool:
        # Search filter (peer URI substring, case-insensitive).
        needle = self._search.text().strip().lower()
        if needle and needle not in (entry.peer_uri or "").lower():
            return False
        # Direction filter
        dir_key = self._dir_filter.currentData()
        if dir_key == "in" and entry.direction != "in":
            return False
        if dir_key == "out" and entry.direction != "out":
            return False
        if dir_key == "missed" and not (
            entry.direction == "in" and not entry.was_answered
        ):
            return False
        # Range filter -- fall back to started_at when ended_at is 0.
        rng = self._range_filter.currentData()
        ts = entry.ended_at or entry.started_at
        if rng != "all" and ts:
            now = datetime.now().date()
            when = datetime.fromtimestamp(ts).date()
            if rng == "today" and when != now:
                return False
            if rng == "yesterday" and when != now - timedelta(days=1):
                return False
            if rng == "week" and now - when > timedelta(days=7):
                return False
            if rng == "month" and now - when > timedelta(days=30):
                return False
        return True

    def _open_detail(self, index: int) -> None:
        # HistoryRow emits its visible-list index (the `i` from the
        # enumerate in _refresh_rows). Previously this method indexed
        # self._entries (the FULL list) with that visible-list value,
        # so opening detail on a filtered row showed the WRONG CDR.
        # Resolve through the visible projection.
        visible = [e for e in self._entries if self._matches_filters(e)]
        if not (0 <= index < len(visible)):
            return
        entry = visible[index]
        dlg = CdrDetailDialog(entry, parent=self)
        dlg.redial_requested.connect(self.redial_requested.emit)
        runner = getattr(dlg, "exec")
        runner()

    def _on_delete_one(self, index: int) -> None:
        from noc_beam.config.history import save_history
        visible = [e for e in self._entries if self._matches_filters(e)]
        if not (0 <= index < len(visible)):
            return
        target = visible[index]
        self._entries = [e for e in self._entries if e is not target]
        try:
            save_history(self._entries)
        except Exception:
            return
        self._refresh_rows()
        self.missed_count_changed.emit(self.unread_missed_count())

    def _on_copy_uri(self, uri: str) -> None:
        from PySide6.QtWidgets import QApplication
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(uri)

    def _on_clear(self) -> None:
        # Confirm before nuking everything.
        reply = QMessageBox.question(
            self,
            "Clear call history",
            "Delete all call history entries? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        clear_history()
        self.reload()
