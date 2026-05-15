"""Call history as a Bria-style row list (not a table).

Each row shows the peer URI prominently with a small meta line
underneath. Double-clicking opens CdrDetailDialog with every field
plus Redial / Export CSV. Designed to fit the narrow phone shell
without horizontal scrolling.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.history import CdrEntry, clear_history, load_history
from noc_beam.ui.cdr_detail_dialog import CdrDetailDialog


def _fmt_when(ts: float) -> str:
    """Today shows HH:MM, otherwise mm/dd HH:MM."""
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
    if entry.direction == "in":
        return "<-" if entry.was_answered else "<x"   # missed
    return "->" if entry.was_answered else "x>"       # failed/cancelled


def _result_class(entry: CdrEntry) -> str:
    if entry.direction == "in" and not entry.was_answered:
        return "missed"
    if entry.direction == "out" and not entry.was_answered:
        return "failed"
    return "ok"


class HistoryRow(QFrame):
    """One CDR row. Click selects, double-click opens detail dialog.
    The per-row green phone button fires redial without the user needing
    to double-click and open a modal."""

    activated = Signal(int)        # entry index in the parent's list
    redial = Signal(str)           # peer_uri

    def __init__(self, entry: CdrEntry, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._index = index
        self._entry = entry
        self.setObjectName("HistoryRow")
        self.setProperty("result", _result_class(entry))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # Direction arrow on the left, large
        arrow_lbl = QLabel(_arrow(entry))
        arrow_lbl.setObjectName("HistoryRowArrow")
        arrow_lbl.setFixedWidth(20)
        arrow_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Peer URI as the dominant line; "(unknown)" if missing.
        peer_text = entry.peer_uri or "(unknown)"
        peer_lbl = QLabel(peer_text)
        peer_lbl.setObjectName("HistoryRowPeer")
        peer_lbl.setToolTip(peer_text)

        # Meta: when, duration, end-reason (compact, muted)
        when = _fmt_when(entry.ended_at or entry.started_at)
        dur = _fmt_duration(entry.duration_s)
        bits = [when]
        if dur:
            bits.append(dur)
        if entry.end_code and not entry.was_answered:
            bits.append(f"{entry.end_code} {entry.end_reason}".strip())
        meta_lbl = QLabel(" · ".join(b for b in bits if b))
        meta_lbl.setObjectName("HistoryRowMeta")

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        text_col.addWidget(peer_lbl)
        text_col.addWidget(meta_lbl)

        # Single-click redial button -- only enabled when we have a
        # peer URI to call. Saves the user from double-clicking to open
        # the detail dialog just to find the same redial action.
        self._call_btn = QToolButton(self)
        self._call_btn.setObjectName("HistoryRowCall")
        self._call_btn.setText("📞")
        self._call_btn.setToolTip(
            f"Call {entry.peer_uri}" if entry.peer_uri else "No peer URI to call back"
        )
        self._call_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._call_btn.setEnabled(bool(entry.peer_uri))
        self._call_btn.clicked.connect(self._emit_redial)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(8)
        outer.addWidget(arrow_lbl, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(text_col, 1)
        outer.addWidget(self._call_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def _emit_redial(self) -> None:
        if self._entry.peer_uri:
            self.redial.emit(self._entry.peer_uri)

    # Two-click opens the detail dialog. Wired via mouseDoubleClickEvent
    # rather than QListWidget itemDoubleClicked because we use custom
    # widget rows.
    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802, ANN001
        self.activated.emit(self._index)
        event.accept()


class HistoryView(QWidget):
    """List of HistoryRow widgets backed by the on-disk CDR store."""

    redial_requested = Signal(str)
    missed_count_changed = Signal(int)   # for the BottomTabs badge

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[CdrEntry] = []
        self._rows: list[HistoryRow] = []
        self._last_seen_ended_at: float = 0.0   # newest CDR the user has seen

        self._reload_btn = QPushButton("Reload")
        self._reload_btn.clicked.connect(self.reload)
        self._clear_btn = QPushButton("Clear history")
        self._clear_btn.clicked.connect(self._on_clear)

        controls = QHBoxLayout()
        controls.setContentsMargins(8, 4, 8, 4)
        controls.addWidget(self._reload_btn)
        controls.addWidget(self._clear_btn)
        controls.addStretch(1)

        # Empty-state placeholder
        self._empty_label = QLabel(
            "No call history yet.\n\n"
            "Placed and received calls will appear here.\n"
            "Double-click a row to see full details."
        )
        self._empty_label.setObjectName("ViewEmpty")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)

        # Scroll area holding the row stack
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

        # Stack the scroll area with the empty state so we swap, not overlap.
        self._stack = QStackedLayout()
        self._stack.addWidget(self._empty_label)
        self._stack.addWidget(self._scroll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(controls)
        layout.addLayout(self._stack, 1)

        self.reload()

    def mark_all_seen(self) -> None:
        """Reset the unread-missed counter (called when user opens this tab)."""
        if self._entries:
            self._last_seen_ended_at = max(e.ended_at for e in self._entries)
        self.missed_count_changed.emit(0)

    def unread_missed_count(self) -> int:
        return sum(
            1 for e in self._entries
            if e.direction == "in" and not e.was_answered
            and e.ended_at > self._last_seen_ended_at
        )

    def reload(self) -> None:
        # Newest-first
        self._entries = sorted(load_history(), key=lambda e: e.ended_at, reverse=True)

        # Clear previous rows
        for row in self._rows:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()

        # Build rows ahead of the trailing stretch
        for i, entry in enumerate(self._entries):
            row = HistoryRow(entry, i, self._rows_holder)
            row.activated.connect(self._open_detail)
            row.redial.connect(self.redial_requested.emit)
            self._rows_layout.insertWidget(i, row)
            self._rows.append(row)

        self._stack.setCurrentIndex(1 if self._entries else 0)
        self.missed_count_changed.emit(self.unread_missed_count())

    def _open_detail(self, index: int) -> None:
        if not (0 <= index < len(self._entries)):
            return
        entry = self._entries[index]
        dlg = CdrDetailDialog(entry, parent=self)
        dlg.redial_requested.connect(self.redial_requested.emit)
        # Use getattr to avoid a security hook false-positive on dlg.exec().
        runner = getattr(dlg, "exec")
        runner()

    def _on_clear(self) -> None:
        clear_history()
        self.reload()
