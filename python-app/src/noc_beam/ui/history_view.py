"""Call history table backed by the persistent CDR store.

Double-clicking a row emits `redial_requested(peer_uri)` so the main window
can place a callback. Refreshed manually via `reload()` (cheap — the JSON
file is small).
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.history import CdrEntry, clear_history, load_history


def _fmt_when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _fmt_duration(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _direction_arrow(entry: CdrEntry) -> str:
    if entry.direction == "in":
        return "← in" if entry.was_answered else "← missed"
    return "→ out" if entry.was_answered else "→ failed"


class HistoryView(QWidget):
    redial_requested = Signal(str)

    COLUMNS = ("When", "Direction", "Peer", "Duration", "Result")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        self._table.itemDoubleClicked.connect(self._on_double_click)

        self._reload_btn = QPushButton("Reload")
        self._reload_btn.clicked.connect(self.reload)
        self._clear_btn = QPushButton("Clear history")
        self._clear_btn.clicked.connect(self._on_clear)

        controls = QHBoxLayout()
        controls.addWidget(self._reload_btn)
        controls.addWidget(self._clear_btn)
        controls.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self._table, 1)

        self.reload()

    def reload(self) -> None:
        entries = sorted(load_history(), key=lambda e: e.ended_at, reverse=True)
        self._table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            cells = [
                _fmt_when(entry.ended_at),
                _direction_arrow(entry),
                entry.peer_uri or "—",
                _fmt_duration(entry.duration_s),
                f"{entry.end_code} {entry.end_reason}".strip() or "—",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, entry.peer_uri)
                self._table.setItem(row, col, item)

    def _on_double_click(self, item: QTableWidgetItem) -> None:
        peer = item.data(Qt.UserRole)
        if peer:
            self.redial_requested.emit(str(peer))

    def _on_clear(self) -> None:
        clear_history()
        self.reload()
