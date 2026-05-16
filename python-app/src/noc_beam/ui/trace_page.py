"""Trace destination -- TraceView wrapped with a polished filter bar.

Sits in the rail's Trace slot. Composition:

  - Top toolbar (#TraceToolbar): RX/TX direction toggles, free-text
    filter, export + clear actions
  - Preset chip row (#TraceChipBar): one-click filters for the SIP
    methods + status classes a NOC engineer reaches for first
    (INVITE / REGISTER / OPTIONS / 4xx / 5xx / BYE)
  - Body: the existing TraceView text pane. Re-uses the TraceView's
    own widgets via reparenting -- the toolbar / chip bar live above
    the original layout so signals stay wired.

The drawer keeps using a bare TraceView (the trace as a companion to
whatever the user is doing). This page is the focused destination
where the user lives during a debugging session.

Tier-3 will add collapsible message groups; Tier-2 stops here.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.ui.trace_view import TraceView


# Preset filter chips. Tuple of (label shown, filter substring set).
# Multi-chip selection ORs the substrings via the existing free-text
# filter -- TraceView only supports a single substring, so we join
# selected chip substrings with " " and pass them as the filter; the
# free-text input takes precedence when present.
# Chip label → TraceView method-chip key. Drives the structural
# method/status filter in TraceView (not the free-text substring
# filter, which was the old hack that matched "192.168.4.x" for the
# 4xx chip and "Content-Length: 451" for any body starting with " 4").
_PRESETS: tuple[tuple[str, str], ...] = (
    ("INVITE",   "INVITE"),
    ("REGISTER", "REGISTER"),
    ("OPTIONS",  "OPTIONS"),
    ("BYE",      "BYE"),
    ("4xx",      "4xx"),
    ("5xx",      "5xx"),
)


class TracePage(QWidget):
    """Wraps TraceView with a polished toolbar and preset chip row."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.trace = TraceView(self)
        self._chip_btns: list[QToolButton] = []
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        # Toolbar -- re-parent TraceView's existing controls into our row.
        toolbar = QFrame(self)
        toolbar.setObjectName("TraceToolbar")
        tb_l = QHBoxLayout(toolbar)
        tb_l.setContentsMargins(16, 10, 16, 10)
        tb_l.setSpacing(10)
        tb_l.addWidget(self.trace.chk_rx)
        tb_l.addWidget(self.trace.chk_tx)
        tb_l.addWidget(self.trace.filter_edit, 1)
        tb_l.addWidget(self.trace.export_btn)
        tb_l.addWidget(self.trace.clear_btn)

        # Chip bar
        chip_bar = QFrame(self)
        chip_bar.setObjectName("TraceChipBar")
        cb_l = QHBoxLayout(chip_bar)
        cb_l.setContentsMargins(16, 8, 16, 8)
        cb_l.setSpacing(6)
        for label, _ in _PRESETS:
            btn = QToolButton(chip_bar)
            btn.setObjectName("TraceChip")
            btn.setText(label)
            btn.setCheckable(True)
            btn.toggled.connect(self._on_chip_toggled)
            self._chip_btns.append(btn)
            cb_l.addWidget(btn)
        cb_l.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(toolbar)
        outer.addWidget(chip_bar)
        outer.addWidget(self.trace, 1)

    # ------------------------------------------------------------------
    def _on_chip_toggled(self, _checked: bool) -> None:
        """Forward the page-level preset chip state to TraceView's own
        structural method chips. Old version stuffed substrings into
        the free-text filter, which produced false positives (the 4xx
        chip matched "192.168.4.x" peers, etc).
        """
        # Mirror each page chip onto the matching TraceView chip and
        # let TraceView's existing matches_method() do the real work.
        for btn, (_label, key) in zip(self._chip_btns, _PRESETS):
            target = self.trace._method_chips.get(key)
            if target is None:
                continue
            if target.isChecked() != btn.isChecked():
                target.blockSignals(True)
                target.setChecked(btn.isChecked())
                target.blockSignals(False)
        # Trigger a single re-filter pass.
        try:
            self.trace._reapply_filters()
        except Exception:
            pass

    # Forward attribute access to the underlying TraceView so existing
    # MainWindow wiring (export_failed signal, etc.) keeps working.
    @property
    def export_failed(self):
        return self.trace.export_failed
