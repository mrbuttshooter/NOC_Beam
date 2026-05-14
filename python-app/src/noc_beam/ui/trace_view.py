"""SIP message trace viewer.

Listens to SipEvents.sip_message and renders a colour-coded log. Supports
filtering by direction and free-text search.
"""
from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from noc_beam.sip.events import sip_events


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
        self.clear_btn = QPushButton("Clear")
        toolbar.addWidget(self.chk_rx)
        toolbar.addWidget(self.chk_tx)
        toolbar.addWidget(self.filter_edit, 1)
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
        sip_events().sip_message.connect(self._on_sip_message)

    def _on_sip_message(self, ts: float, direction: str, peer: str, body: str) -> None:
        if direction == "RX" and not self.chk_rx.isChecked():
            return
        if direction == "TX" and not self.chk_tx.isChecked():
            return
        flt = self.filter_edit.text().strip().lower()
        if flt and flt not in body.lower() and flt not in peer.lower():
            return

        cursor = self.text.textCursor()
        cursor.movePosition(QTextCursor.End)

        when = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
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
