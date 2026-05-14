"""Live list of currently-active calls.

Subscribes to `CallManager` signals and keeps a `QListWidget` in sync. The
parent window drives selection -> call_widget routing.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout, QLabel, QWidget

from noc_beam.sip.call_manager import CallManager, CallState


_STATE_PRETTY = {
    CallState.NULL: "—",
    CallState.CALLING: "Calling…",
    CallState.INCOMING: "Incoming",
    CallState.EARLY: "Ringing",
    CallState.CONNECTING: "Connecting",
    CallState.CONFIRMED: "In call",
    CallState.HELD: "On hold",
    CallState.DISCONNECTED: "Ended",
}


class CallListWidget(QWidget):
    """A compact list of active calls. Emits the pjsua2 call-id on selection."""

    call_selected = Signal(int)

    def __init__(self, manager: CallManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._manager = manager

        self._list = QListWidget()
        self._list.setObjectName("CallList")
        self._list.itemSelectionChanged.connect(self._on_selection_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Active calls"))
        layout.addWidget(self._list, 1)

        manager.call_added.connect(self._refresh)
        manager.call_updated.connect(self._refresh)
        manager.call_removed.connect(self._refresh)
        self._refresh()

    def _row_text(self, call_id: int) -> str:
        rec = self._manager.get(call_id)
        if rec is None:
            return f"#{call_id} (gone)"
        arrow = "←" if rec.direction == "in" else "→"
        peer = rec.remote_uri or "unknown"
        state = _STATE_PRETTY.get(rec.state, rec.state.value)
        muted = " 🔇" if rec.muted else ""
        return f"{arrow} {peer}   [{state}]{muted}"

    def _refresh(self, *_args: object) -> None:
        prev_selected = self.selected_call_id()
        self._list.blockSignals(True)
        self._list.clear()
        for rec in self._manager.all():
            item = QListWidgetItem(self._row_text(rec.call_id))
            item.setData(Qt.UserRole, rec.call_id)
            self._list.addItem(item)
            if rec.call_id == prev_selected:
                self._list.setCurrentItem(item)
        self._list.blockSignals(False)
        if prev_selected is None and self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _on_selection_changed(self) -> None:
        cid = self.selected_call_id()
        if cid is not None:
            self.call_selected.emit(cid)

    def selected_call_id(self) -> int | None:
        item = self._list.currentItem()
        if item is None:
            return None
        cid = item.data(Qt.UserRole)
        return int(cid) if cid is not None else None
