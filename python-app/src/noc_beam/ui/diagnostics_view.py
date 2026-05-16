"""Diagnostics destination -- the NOC primitives v2 didn't model.

Six sub-panels, each tied to data we already collect (or can collect
trivially from the existing event surface):

  - OPTIONS probe        SIP URI -> RTT for an OPTIONS round-trip
  - REGISTER timing      Per-account REGISTER attempt history + RTT
  - ICE / STUN           Per-account STUN config + candidate readout
  - TLS cert chain       Per-account transport + cert summary (when TLS)
  - RTCP-XR voice        Live MOS / loss / jitter / RTT for the
                         currently-selected call

Most panels degrade gracefully when their data source isn't wired up
yet -- they show a one-line explanation rather than crashing or
faking values, because faked diagnostics are worse than no diagnostics.
"""
from __future__ import annotations

import logging
import time
from collections import deque

from PySide6 import QtGui
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.store import AccountConfig
from noc_beam.sip.events import sip_events

log = logging.getLogger(__name__)


# Cap on rows kept in memory per panel. The REGISTER history is the
# rolling 30-day spec but for a desktop app we cap on row count to
# keep widget repaint cost predictable; 30 days at one REGISTER per
# 5 min per account is ~8.6k rows -- 5000 is plenty.
_MAX_ROWS = 5000


def _section_title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("ViewTitle")
    return lbl


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("DiagHint")
    lbl.setWordWrap(True)
    return lbl


def _value_row(form: QFormLayout, label: str, value: str) -> QLabel:
    val = QLabel(value)
    val.setObjectName("DiagValue")
    val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    form.addRow(label, val)
    return val


class _OptionsProbePanel(QWidget):
    probe_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        layout.addWidget(_section_title("OPTIONS probe"))
        layout.addWidget(_hint(
            "Sends a SIP OPTIONS request to a target URI and reports "
            "the response code and round-trip time. Useful for sanity-"
            "checking reachability and measuring registrar RTT."
        ))

        row = QHBoxLayout()
        self.target = QLineEdit()
        self.target.setPlaceholderText("sip:proxy.example.com:5060")
        self.go_btn = QPushButton("Send OPTIONS")
        self.go_btn.clicked.connect(self._on_go)
        row.addWidget(self.target, 1)
        row.addWidget(self.go_btn)
        layout.addLayout(row)

        self.results = QTextEdit()
        self.results.setObjectName("DiagResults")
        self.results.setReadOnly(True)
        layout.addWidget(self.results, 1)

    def _on_go(self) -> None:
        target = self.target.text().strip()
        if not target:
            return
        when = time.strftime("%H:%M:%S")
        # Try the SipEndpoint helper if it exists; otherwise be honest.
        try:
            from noc_beam.sip.endpoint import SipEndpoint

            ep = SipEndpoint.instance()
            probe = getattr(ep, "options_probe", None)
            if callable(probe):
                code, reason, rtt_ms = probe(target)
                self.results.append(
                    f"[{when}] {target}  →  {code} {reason}  ({rtt_ms:.1f} ms)"
                )
            else:
                self.results.append(
                    f"[{when}] {target}  →  pjsua2 OPTIONS probe not yet wired "
                    "in SipEndpoint (add SipEndpoint.options_probe)."
                )
        except Exception as e:
            log.exception("OPTIONS probe failed")
            self.results.append(f"[{when}] {target}  →  error: {e}")
        self.probe_requested.emit(target)


class _RegisterTimingPanel(QWidget):
    """Listens to registration_changed, accumulates per-account timing."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        layout.addWidget(_section_title("REGISTER timing"))
        layout.addWidget(_hint(
            "Per-account REGISTER attempt log with response code, time "
            "since the previous attempt, and the elapsed wall clock. "
            "Useful for spotting registrar misbehaviour, retry storms, "
            "or auth churn."
        ))

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Time", "Account", "Code", "Δ since prev"]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

        self._last_seen: dict[str, float] = {}
        sip_events().registration_changed.connect(self._on_reg)

    def _on_reg(self, account_id: str, code: int, reason: str) -> None:
        if code == 0:
            return
        now = time.time()
        prev = self._last_seen.get(account_id)
        delta = f"{(now - prev) * 1000:.0f} ms" if prev is not None else "-"
        self._last_seen[account_id] = now

        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(time.strftime("%H:%M:%S")))
        self.table.setItem(row, 1, QTableWidgetItem(account_id))
        code_item = QTableWidgetItem(f"{code} {reason}")
        # Use the same palette tokens dark/dark-hc/light.qss already
        # define for status colors -- raw Qt.GlobalColor.green is
        # fixed pure-RGB and is unreadable on light backgrounds plus
        # clashes with the muted Bria-cyan palette on dark.
        if 200 <= code < 300:
            code_item.setForeground(QtGui.QColor("#66D19E"))  # success token
        elif code in (401, 403, 407):
            code_item.setForeground(QtGui.QColor("#EF5350"))  # danger token
        self.table.setItem(row, 2, code_item)
        self.table.setItem(row, 3, QTableWidgetItem(delta))

        if self.table.rowCount() > _MAX_ROWS:
            self.table.removeRow(0)
        self.table.scrollToBottom()


class _IceStunPanel(QWidget):
    """Reads STUN config per account from AccountConfig.

    Live ICE candidate enumeration would require a pjsua2 hook
    (account.getInfo() exposes regUri but not the active candidates);
    this panel presents the configured surface today and is a clean
    drop-in spot for the candidate table once the SDK side lands.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        layout.addWidget(_section_title("ICE / STUN"))
        layout.addWidget(_hint(
            "STUN servers configured per account. ICE candidate "
            "enumeration is wired to the SipEndpoint when the SDK "
            "exposes per-account candidate info; until then this "
            "panel reports the static config."
        ))

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ["Account", "Transport", "STUN server"]
        )
        for col in range(3):
            self.table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.Stretch
            )
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

    def update_accounts(self, accounts: list[AccountConfig]) -> None:
        self.table.setRowCount(0)
        for acc in accounts:
            if not acc.enabled:
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            label = acc.display_name or f"{acc.username}@{acc.domain}"
            self.table.setItem(row, 0, QTableWidgetItem(label))
            self.table.setItem(row, 1, QTableWidgetItem(acc.transport.upper()))
            stun = acc.stun_server or "(none)"
            self.table.setItem(row, 2, QTableWidgetItem(stun))


class _TlsCertPanel(QWidget):
    """Placeholder for the TLS cert chain viewer.

    pjsua2 exposes the negotiated transport but cert details are not
    surfaced through pjsua2 cleanly. We list each TLS account here so
    the user has a stable surface; the full cert chain depends on the
    SDK side and lands when SipEndpoint exposes it.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        layout.addWidget(_section_title("TLS cert chain"))
        layout.addWidget(_hint(
            "Per-account TLS transport status. Cert chain detail is "
            "pending an SDK helper -- the panel will populate once "
            "SipEndpoint exposes cert info."
        ))

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ["Account", "Transport", "Status"]
        )
        for col in range(3):
            self.table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.Stretch
            )
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

    def update_accounts(self, accounts: list[AccountConfig]) -> None:
        self.table.setRowCount(0)
        for acc in accounts:
            if not acc.enabled:
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            label = acc.display_name or f"{acc.username}@{acc.domain}"
            transport = acc.transport.upper()
            status = "TLS active" if acc.transport == "tls" else "non-TLS"
            self.table.setItem(row, 0, QTableWidgetItem(label))
            self.table.setItem(row, 1, QTableWidgetItem(transport))
            self.table.setItem(row, 2, QTableWidgetItem(status))


class _RtcpXrPanel(QWidget):
    """Live MOS / loss / jitter / RTT for the currently-selected call."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        layout.addWidget(_section_title("RTCP-XR voice metrics"))
        layout.addWidget(_hint(
            "Live MOS, packet loss, jitter and round-trip time for the "
            "currently-selected call. Sampled every 2 seconds by "
            "CallQualitySampler. Idle when no call is active."
        ))

        form = QFormLayout()
        form.setHorizontalSpacing(20)
        self.call_id_lbl = _value_row(form, "Call ID", "-")
        self.mos_lbl = _value_row(form, "MOS", "-")
        self.loss_lbl = _value_row(form, "Loss", "-")
        self.jitter_lbl = _value_row(form, "Jitter", "-")
        self.rtt_lbl = _value_row(form, "RTT", "-")
        layout.addLayout(form)

        # Tiny rolling history of the last 10 samples
        self.history = QTableWidget(0, 5)
        self.history.setHorizontalHeaderLabels(
            ["Time", "MOS", "Loss %", "Jitter ms", "RTT ms"]
        )
        for col in range(5):
            self.history.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.Stretch
            )
        self.history.verticalHeader().setVisible(False)
        layout.addWidget(self.history, 1)

        self._buf: deque[tuple[float, float, float, float, float]] = deque(maxlen=120)
        self._selected_call_id: int | None = None
        sip_events().call_quality.connect(self._on_quality)

    def set_selected_call(self, call_id: int | None) -> None:
        self._selected_call_id = call_id
        self.call_id_lbl.setText(str(call_id) if call_id is not None else "-")
        if call_id is None:
            for lbl in (self.mos_lbl, self.loss_lbl, self.jitter_lbl, self.rtt_lbl):
                lbl.setText("-")
            self.history.setRowCount(0)
            self._buf.clear()

    def _on_quality(self, call_id: int, mos: float, loss: float,
                    jitter_ms: float, rtt_ms: float) -> None:
        if self._selected_call_id is not None and call_id != self._selected_call_id:
            return
        self.call_id_lbl.setText(str(call_id))
        self.mos_lbl.setText(f"{mos:.2f}")
        self.loss_lbl.setText(f"{loss:.2f} %")
        self.jitter_lbl.setText(f"{jitter_ms:.1f} ms")
        self.rtt_lbl.setText(f"{rtt_ms:.1f} ms")

        ts = time.time()
        self._buf.append((ts, mos, loss, jitter_ms, rtt_ms))
        row = self.history.rowCount()
        self.history.insertRow(row)
        self.history.setItem(row, 0, QTableWidgetItem(time.strftime("%H:%M:%S")))
        self.history.setItem(row, 1, QTableWidgetItem(f"{mos:.2f}"))
        self.history.setItem(row, 2, QTableWidgetItem(f"{loss:.2f}"))
        self.history.setItem(row, 3, QTableWidgetItem(f"{jitter_ms:.1f}"))
        self.history.setItem(row, 4, QTableWidgetItem(f"{rtt_ms:.1f}"))
        if self.history.rowCount() > 120:
            self.history.removeRow(0)
        self.history.scrollToBottom()


class DiagnosticsView(QWidget):
    """Tabbed view for the NOC diagnostic primitives."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(16, 12, 16, 8)
        title_row.addWidget(_section_title("Diagnostics"))
        title_row.addStretch(1)
        layout.addLayout(title_row)

        self.tabs = QTabWidget()
        self.options_panel = _OptionsProbePanel()
        self.register_panel = _RegisterTimingPanel()
        self.ice_panel = _IceStunPanel()
        self.tls_panel = _TlsCertPanel()
        self.rtcp_panel = _RtcpXrPanel()
        self.tabs.addTab(self.options_panel, "OPTIONS probe")
        self.tabs.addTab(self.register_panel, "REGISTER timing")
        self.tabs.addTab(self.ice_panel, "ICE / STUN")
        self.tabs.addTab(self.tls_panel, "TLS cert")
        self.tabs.addTab(self.rtcp_panel, "RTCP-XR")
        layout.addWidget(self.tabs, 1)

    # ------------------------------------------------------------------
    def update_accounts(self, accounts: list[AccountConfig]) -> None:
        self.ice_panel.update_accounts(accounts)
        self.tls_panel.update_accounts(accounts)

    def set_selected_call(self, call_id: int | None) -> None:
        self.rtcp_panel.set_selected_call(call_id)
