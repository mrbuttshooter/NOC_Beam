"""SIP message trace viewer.

Listens to SipEvents.sip_message and renders a colour-coded log. Supports
filtering by direction and free-text search. Trace messages are also
appended to a rotating log file under the user data dir so they survive
restarts.
"""
from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler

from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.paths import log_dir
from noc_beam.sip.events import sip_events


_trace_logger: logging.Logger | None = None


def _persistent_trace_logger() -> logging.Logger:
    """Lazily build a rotating file logger for SIP messages.

    5 MB per file × 5 files = ~25 MB cap. Lives under the platform's user
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


class TraceView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        toolbar = QHBoxLayout()
        self.chk_rx = QCheckBox("RX")
        self.chk_rx.setChecked(True)
        self.chk_tx = QCheckBox("TX")
        self.chk_tx.setChecked(True)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter (e.g. INVITE, 401, alice@example.com)")
        self.export_btn = QPushButton("Export…")
        self.clear_btn = QPushButton("Clear")
        toolbar.addWidget(self.chk_rx)
        toolbar.addWidget(self.chk_tx)
        toolbar.addWidget(self.filter_edit, 1)
        toolbar.addWidget(self.export_btn)
        toolbar.addWidget(self.clear_btn)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        mono = QFont("Cascadia Mono")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(9)
        self.text.setFont(mono)
        self.text.document().setMaximumBlockCount(5000)

        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(self.text)

        self.clear_btn.clicked.connect(self.text.clear)
        self.export_btn.clicked.connect(self._on_export)
        sip_events().sip_message.connect(self._on_sip_message)

    def _on_sip_message(self, ts: float, direction: str, peer: str, body: str) -> None:
        when = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        # Persistent record first — independent of UI filters so traces
        # survive even when the user is filtering noise out of the view.
        try:
            _persistent_trace_logger().info(
                "[%s] %s  %s\n%s", when, direction, peer, body,
            )
        except Exception:
            pass

        # UI filters (direction toggles + free-text)
        if direction == "RX" and not self.chk_rx.isChecked():
            return
        if direction == "TX" and not self.chk_tx.isChecked():
            return
        flt = self.filter_edit.text().strip().lower()
        if flt and flt not in body.lower() and flt not in peer.lower():
            return

        cursor = self.text.textCursor()
        cursor.movePosition(QTextCursor.End)

        header_fmt = QTextCharFormat()
        body_fmt = QTextCharFormat()
        if direction == "RX":
            header_fmt.setForeground(QColor("#7FD3FF"))
        else:
            header_fmt.setForeground(QColor("#FFB86C"))
        body_fmt.setForeground(QColor("#E0E0E0"))

        cursor.insertText(f"\n[{when}] {direction}  {peer}\n", header_fmt)
        cursor.insertText(body + "\n", body_fmt)

        self.text.setTextCursor(cursor)
        self.text.ensureCursorVisible()

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
                fh.write(self.text.toPlainText())
        except Exception as e:
            logging.getLogger(__name__).exception("Trace export failed")
            self.window().statusBar().showMessage(f"Export failed: {e}", 5000)
