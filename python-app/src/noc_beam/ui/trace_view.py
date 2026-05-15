"""SIP message trace viewer (Wireshark-style row list).

Each SIP message gets its own QFrame row instead of being appended to a
QTextEdit blob. Rows have:

  - 3 px left band, colour-coded by direction (cyan = RX, amber = TX,
    red overlay if the body looks like a >= 400 response)
  - timestamp + direction tag in mono
  - method + first response-line (e.g. "INVITE sip:..." or "200 OK")
  - peer URI (right-aligned, ellided)
  - click to expand the full message body inline

The view caps at MAX_ROWS so a long-running session doesn't accumulate
indefinitely; oldest row gets dropped first. Filters re-render row
visibility without rebuilding (so a filter change is cheap).

Public API kept identical to the previous QTextEdit version so the
toolbar widgets (chk_rx, chk_tx, filter_edit, export_btn, clear_btn)
can still be re-parented by TracePage. export_failed signal still
fires for the rail status pill.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.paths import log_dir
from noc_beam.sip.events import sip_events


_trace_logger: logging.Logger | None = None
MAX_ROWS = 500


def _persistent_trace_logger() -> logging.Logger:
    """Lazily build a rotating file logger for SIP messages.

    5 MB per file x 5 files = ~25 MB cap. Lives under the platform's user
    log dir; the trace UI can be cleared independently.
    """
    global _trace_logger
    if _trace_logger is not None:
        return _trace_logger
    logger = logging.getLogger("noc_beam.sip.trace.file")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_dir() / "sip_trace.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    _trace_logger = logger
    return logger


@dataclass
class _Msg:
    ts: float
    direction: str        # "RX" or "TX"
    peer: str
    body: str
    when: str             # pre-formatted hh:mm:ss.mmm
    summary: str          # first line of body (request line or status)
    is_error: bool        # body parses as >= 400


def _summarize(body: str) -> tuple[str, bool]:
    """First line of the SIP message + is-error flag."""
    if not body:
        return "(empty)", False
    first = body.split("\n", 1)[0].strip()
    is_error = False
    # status line: "SIP/2.0 401 Unauthorized"
    if first.startswith("SIP/"):
        parts = first.split(None, 2)
        if len(parts) >= 2:
            try:
                code = int(parts[1])
                is_error = code >= 400
            except ValueError:
                pass
    return first, is_error


class TraceRow(QFrame):
    """One Wireshark-style row -- colour-coded band + meta + expand."""

    def __init__(self, msg: _Msg, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.msg = msg
        self.setObjectName("TraceRow")
        # Dynamic property drives the QSS branch for colour band + tint.
        if msg.is_error:
            level = "error"
        elif msg.direction == "RX":
            level = "rx"
        else:
            level = "tx"
        self.setProperty("dir", level)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # ---- Header row -----------------------------------------------
        ts_lbl = QLabel(msg.when, self)
        ts_lbl.setObjectName("TraceRowTime")

        dir_lbl = QLabel(msg.direction, self)
        dir_lbl.setObjectName("TraceRowDir")
        dir_lbl.setProperty("dir", level)

        summary_lbl = QLabel(msg.summary, self)
        summary_lbl.setObjectName("TraceRowSummary")
        summary_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        peer_lbl = QLabel(msg.peer, self)
        peer_lbl.setObjectName("TraceRowPeer")
        peer_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        head = QHBoxLayout()
        head.setContentsMargins(10, 6, 10, 6)
        head.setSpacing(10)
        head.addWidget(ts_lbl)
        head.addWidget(dir_lbl)
        head.addWidget(summary_lbl, 1)
        head.addWidget(peer_lbl, 0)

        # ---- Body (hidden by default, shown when row is expanded) -----
        self.body = QTextEdit(self)
        self.body.setObjectName("TraceRowBody")
        self.body.setReadOnly(True)
        self.body.setPlainText(msg.body)
        self.body.setVisible(False)
        # Tight body height -- shrinks to content; max ~240 px so a giant
        # SDP doesn't take over the viewport
        self.body.document().adjustSize()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(head)
        outer.addWidget(self.body)

    # Click anywhere on the row toggles the body.
    def mousePressEvent(self, event):  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.body.setVisible(not self.body.isVisible())
        super().mousePressEvent(event)


class TraceView(QWidget):
    export_failed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # ---- Toolbar widgets (kept on this widget so TracePage can
        # re-parent them into a polished toolbar). chk_rx / chk_tx are
        # QToolButton(checkable) instead of QCheckBox so the chip
        # styling lands cleanly -- QCheckBox's indicator fights every
        # `width: 0` rule even with `image: none`.
        self.chk_rx = QToolButton()
        self.chk_rx.setText("RX")
        self.chk_rx.setCheckable(True)
        self.chk_rx.setChecked(True)
        self.chk_rx.setObjectName("TraceDirToggle")
        self.chk_rx.setProperty("dir", "rx")
        self.chk_tx = QToolButton()
        self.chk_tx.setText("TX")
        self.chk_tx.setCheckable(True)
        self.chk_tx.setChecked(True)
        self.chk_tx.setObjectName("TraceDirToggle")
        self.chk_tx.setProperty("dir", "tx")
        self.filter_edit = QLineEdit()
        self.filter_edit.setObjectName("TraceFilter")
        self.filter_edit.setPlaceholderText("Filter (e.g. INVITE, 401, alice@example.com)")
        self.export_btn = QPushButton("Export...")
        self.clear_btn = QPushButton("Clear")

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.chk_rx)
        toolbar.addWidget(self.chk_tx)
        toolbar.addWidget(self.filter_edit, 1)
        toolbar.addWidget(self.export_btn)
        toolbar.addWidget(self.clear_btn)

        # ---- Scroll area for the row list ----------------------------
        self._rows: deque[TraceRow] = deque(maxlen=MAX_ROWS)

        self._rows_holder = QFrame(self)
        self._rows_holder.setObjectName("TraceRowList")
        self._rows_layout = QVBoxLayout(self._rows_holder)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch(1)

        # Empty-state label, shown when no rows have arrived yet.
        self._empty = QLabel(
            "Waiting for SIP traffic.\n\n"
            "Once a SIP account is registered or a call is placed,\n"
            "every signalling message will land here.",
            self._rows_holder,
        )
        self._empty.setObjectName("TraceEmpty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Insert before the trailing stretch so the stretch keeps the
        # label centered vertically.
        self._rows_layout.insertWidget(0, self._empty)

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("TraceScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._rows_holder)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(toolbar)
        layout.addWidget(self._scroll, 1)

        # ---- Wires ----------------------------------------------------
        self.clear_btn.clicked.connect(self._on_clear)
        self.export_btn.clicked.connect(self._on_export)
        self.chk_rx.toggled.connect(self._reapply_filters)
        self.chk_tx.toggled.connect(self._reapply_filters)
        self.filter_edit.textChanged.connect(self._reapply_filters)
        sip_events().sip_message.connect(self._on_sip_message)

    # ------------------------------------------------------------------
    def _on_sip_message(self, ts: float, direction: str, peer: str, body: str) -> None:
        when = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        # Persistent record first -- independent of UI filters so traces
        # survive even when the user is filtering noise out of the view.
        try:
            _persistent_trace_logger().info(
                "[%s] %s  %s\n%s", when, direction, peer, body,
            )
        except Exception:
            pass

        summary, is_error = _summarize(body)
        msg = _Msg(ts=ts, direction=direction, peer=peer, body=body,
                   when=when, summary=summary, is_error=is_error)
        row = TraceRow(msg, self._rows_holder)
        # Insert before the trailing stretch (last item).
        insert_at = self._rows_layout.count() - 1
        self._rows_layout.insertWidget(insert_at, row)
        # Drop oldest when over cap (deque handles eviction; we delete
        # the corresponding widget too).
        if len(self._rows) >= MAX_ROWS:
            old = self._rows.popleft()
            old.deleteLater()
        self._rows.append(row)
        # Hide the empty state once the first row arrives.
        if self._empty.isVisible():
            self._empty.setVisible(False)
        # Apply current filters to the new row.
        self._apply_filter_to(row)
        # Auto-scroll to bottom for live feel.
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_clear(self) -> None:
        for row in list(self._rows):
            row.deleteLater()
        self._rows.clear()
        self._empty.setVisible(True)

    # ------------------------------------------------------------------
    def _reapply_filters(self) -> None:
        for row in self._rows:
            self._apply_filter_to(row)

    def _apply_filter_to(self, row: TraceRow) -> None:
        m = row.msg
        # Direction toggles
        if m.direction == "RX" and not self.chk_rx.isChecked():
            row.setVisible(False)
            return
        if m.direction == "TX" and not self.chk_tx.isChecked():
            row.setVisible(False)
            return
        # Free-text filter (case-insensitive on body + peer)
        flt = self.filter_edit.text().strip().lower()
        if flt and flt not in m.body.lower() and flt not in m.peer.lower():
            row.setVisible(False)
            return
        row.setVisible(True)

    # ------------------------------------------------------------------
    def _on_export(self) -> None:
        """Save the currently-visible trace buffer to a .log file."""
        default = log_dir() / f"sip_trace_export_{datetime.now():%Y%m%d_%H%M%S}.log"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export current trace", str(default), "Log files (*.log);;All files (*.*)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for row in self._rows:
                    if not row.isVisible():
                        continue
                    m = row.msg
                    fh.write(f"[{m.when}] {m.direction}  {m.peer}\n{m.body}\n\n")
        except Exception as e:
            logging.getLogger(__name__).exception("Trace export failed")
            self.export_failed.emit(f"Export failed: {e}")
