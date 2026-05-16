"""Accounts detail pane.

Right-hand side of the Accounts master/detail layout. For the currently
selected account it shows:

  - header: avatar (initials), display name + URI, transport / SRTP /
    auth badges, action buttons (Test, Edit, Unregister, Delete)
  - status section: 4-card grid (Uptime, Calls, MOS, RTT)
  - last incident block: most recent non-2xx registration_changed event,
    rendered like the .last-error in accounts.html

Data sources:
  - AccountConfig (passed in via show_account)
  - sip_events.registration_changed for status code -> last incident
  - sip_events.call_quality for the MOS / RTT figures (averaged over a
    rolling 60-sample window per-call)

Empty state: when no account is selected, shows a quiet "Select an
account" hint instead of leaving a wall of dashes.
"""
from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.store import AccountConfig
from noc_beam.sip.events import sip_events


def _initials(name: str, fallback: str = "?") -> str:
    """Two-letter avatar text. Walks display name, falls back to URI local."""
    parts = [p for p in name.replace("_", " ").replace(".", " ").split() if p]
    if not parts:
        return fallback[:2].upper()
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def _badge(text: str, level: str = "neutral") -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setObjectName("AcctBadge")
    if level != "neutral":
        lbl.setProperty("level", level)
    return lbl


def _stat_card(label: str, initial_value: str = "—") -> tuple[QFrame, QLabel]:
    card = QFrame()
    card.setObjectName("StatCard")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(2)
    lbl = QLabel(label)
    lbl.setObjectName("StatLabel")
    val = QLabel(initial_value)
    val.setObjectName("StatValue")
    layout.addWidget(lbl)
    layout.addWidget(val)
    return card, val


class AccountDetail(QWidget):
    """Right pane. Bind via show_account(cfg); clear via show_empty()."""

    edit_requested = Signal()
    remove_requested = Signal()
    test_requested = Signal()
    unregister_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._account: AccountConfig | None = None
        # Per-call quality buffer: call_id -> deque of (ts, mos, loss, jitter, rtt)
        self._quality_buf: dict[int, deque] = {}
        # call_id -> account_id mapping. Without this the stats
        # aggregator can't tell whose call's MOS to show in the
        # detail pane. Populated from sip_events.call_incoming and
        # the call_manager.call_added signal.
        self._call_account: dict[int, str] = {}
        # Last non-2xx event per account_id: (ts, code, reason)
        self._last_incident: dict[str, tuple[float, int, str]] = {}

        self._stack = QStackedLayout(self)
        self._stack.addWidget(self._build_empty())
        self._stack.addWidget(self._build_detail())
        self._stack.setCurrentIndex(0)

        sip_events().registration_changed.connect(self._on_reg_changed)
        sip_events().call_quality.connect(self._on_call_quality)
        # Disconnect on destruction so the singleton sip_events doesn't
        # keep firing into a dead AccountDetail when the accounts
        # window is closed and re-opened (signal-leak audit fix).
        self.destroyed.connect(self._disconnect_signals)

    def _disconnect_signals(self, *_args) -> None:
        try:
            sip_events().registration_changed.disconnect(self._on_reg_changed)
        except Exception:
            pass
        try:
            sip_events().call_quality.disconnect(self._on_call_quality)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _build_empty(self) -> QWidget:
        empty = QWidget()
        layout = QVBoxLayout(empty)
        layout.setContentsMargins(40, 40, 40, 40)
        hint = QLabel("Select an account to see registration status and quality.")
        hint.setObjectName("ViewHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        layout.addStretch(1)
        layout.addWidget(hint)
        layout.addStretch(2)
        return empty

    def _build_detail(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(20)

        # Header row: avatar + name/uri + badges + actions
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(16)

        self.avatar = QLabel("--")
        self.avatar.setObjectName("AcctAvatar")
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)

        ident = QVBoxLayout()
        ident.setContentsMargins(0, 0, 0, 0)
        ident.setSpacing(2)
        self.name_lbl = QLabel("")
        self.name_lbl.setObjectName("AcctName")
        self.uri_lbl = QLabel("")
        self.uri_lbl.setObjectName("AcctUri")
        self.uri_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        ident.addWidget(self.name_lbl)
        ident.addWidget(self.uri_lbl)
        # Badges on a third row
        self.badges_row = QHBoxLayout()
        self.badges_row.setContentsMargins(0, 6, 0, 0)
        self.badges_row.setSpacing(6)
        ident.addLayout(self.badges_row)
        header.addLayout(ident, 1)

        # Action buttons (right of header)
        actions = QVBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        actions_row = QHBoxLayout()
        actions_row.setSpacing(6)
        self.test_btn = QPushButton("Test")
        self.edit_btn = QPushButton("Edit")
        self.unreg_btn = QPushButton("Unregister")
        self.delete_btn = QPushButton("Delete")
        for b in (self.test_btn, self.edit_btn, self.unreg_btn, self.delete_btn):
            actions_row.addWidget(b)
        self.test_btn.clicked.connect(self.test_requested.emit)
        self.edit_btn.clicked.connect(self.edit_requested.emit)
        self.unreg_btn.clicked.connect(self.unregister_requested.emit)
        self.delete_btn.clicked.connect(self.remove_requested.emit)
        actions.addLayout(actions_row)
        actions.addStretch(1)
        header.addLayout(actions, 0)

        outer.addLayout(header)

        # Stats grid (4 cards)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self.uptime_card, self.uptime_val = _stat_card("Uptime")
        self.calls_card, self.calls_val = _stat_card("Calls")
        self.mos_card, self.mos_val = _stat_card("MOS")
        self.rtt_card, self.rtt_val = _stat_card("RTT")
        grid.addWidget(self.uptime_card, 0, 0)
        grid.addWidget(self.calls_card, 0, 1)
        grid.addWidget(self.mos_card, 0, 2)
        grid.addWidget(self.rtt_card, 0, 3)
        outer.addLayout(grid)

        # Last incident block
        self.incident = QFrame()
        self.incident.setObjectName("LastIncident")
        inc_l = QVBoxLayout(self.incident)
        inc_l.setContentsMargins(14, 12, 14, 12)
        inc_l.setSpacing(4)
        self.incident_title = QLabel("Last incident")
        self.incident_title.setObjectName("IncidentTitle")
        self.incident_body = QLabel("")
        self.incident_body.setObjectName("DiagValue")
        self.incident_body.setWordWrap(True)
        inc_l.addWidget(self.incident_title)
        inc_l.addWidget(self.incident_body)
        outer.addWidget(self.incident)
        self.incident.hide()

        outer.addStretch(1)
        return page

    # ------------------------------------------------------------------
    def show_empty(self) -> None:
        self._account = None
        self._stack.setCurrentIndex(0)

    def show_account(self, cfg: AccountConfig) -> None:
        self._account = cfg
        display = cfg.display_name or f"{cfg.username}@{cfg.domain}"
        uri = f"sip:{cfg.username}@{cfg.domain}"
        self.avatar.setText(_initials(display, fallback=cfg.username))
        self.name_lbl.setText(display)
        self.uri_lbl.setText(uri)
        self._refresh_badges(cfg)
        self._refresh_stats(cfg)
        self._refresh_incident(cfg.id)
        self._stack.setCurrentIndex(1)

    def _refresh_badges(self, cfg: AccountConfig) -> None:
        # Clear current badges
        while self.badges_row.count():
            item = self.badges_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        # Transport
        self.badges_row.addWidget(_badge(cfg.transport.upper(),
                                         level="ok" if cfg.transport == "tls" else "neutral"))
        # SRTP
        if cfg.srtp != "disabled":
            self.badges_row.addWidget(_badge(f"SRTP {cfg.srtp}", level="ok"))
        # Auth user
        if cfg.auth_user and cfg.auth_user != cfg.username:
            self.badges_row.addWidget(_badge(f"auth {cfg.auth_user}", level="neutral"))
        # Disabled
        if not cfg.enabled:
            self.badges_row.addWidget(_badge("disabled", level="warn"))
        self.badges_row.addStretch(1)

    def _refresh_stats(self, cfg: AccountConfig) -> None:
        # Uptime / Calls counts not tracked anywhere stable yet -- leave em-dash.
        self.uptime_val.setText("—")
        self.calls_val.setText("—")
        # Per-account MOS / RTT. Previously this averaged across ALL
        # active call quality buffers globally, so two simultaneous
        # accounts each with a call showed the SAME mixed average in
        # both detail panes (mis-attribution). Now filter to the
        # call_ids known to belong to this account via the
        # _call_account map maintained from call_added.
        mos_vals: list[float] = []
        rtt_vals: list[float] = []
        target_id = cfg.id
        for call_id, buf in self._quality_buf.items():
            owner = self._call_account.get(call_id)
            # Match samples whose owner is THIS account. For samples
            # whose owner mapping hasn't landed yet (the first ~1-2
            # ticks after a call is added), include them ONLY when
            # this is the only account currently shown -- prevents
            # cross-account leakage on multi-account setups but
            # avoids the first-second "—" gap a strict drop produced.
            if owner is None:
                # Sole-account fallback: if there's only one ownership
                # mapping in flight and it points here, count the
                # unknown samples too. Otherwise skip to avoid
                # mis-attribution.
                known_owners = set(self._call_account.values())
                if known_owners and known_owners != {target_id}:
                    continue
            elif owner != target_id:
                continue
            for _, mos, _loss, _jit, rtt in buf:
                mos_vals.append(mos)
                rtt_vals.append(rtt)
        if mos_vals:
            self.mos_val.setText(f"{sum(mos_vals) / len(mos_vals):.2f}")
        else:
            self.mos_val.setText("—")
        if rtt_vals:
            self.rtt_val.setText(f"{sum(rtt_vals) / len(rtt_vals):.0f} ms")
        else:
            self.rtt_val.setText("—")

    def note_call_account(self, call_id: int, account_id: str) -> None:
        """Host wires this from CallManager.call_added so we can
        attribute per-call quality samples to the right account."""
        self._call_account[call_id] = account_id

    def forget_call_account(self, call_id: int) -> None:
        self._call_account.pop(call_id, None)
        self._quality_buf.pop(call_id, None)

    def _refresh_incident(self, account_id: str) -> None:
        info = self._last_incident.get(account_id)
        if info is None:
            self.incident.hide()
            return
        ts, code, reason = info
        when = time.strftime("%H:%M:%S", time.localtime(ts))
        self.incident_body.setText(f"[{when}]  {code}  {reason}")
        self.incident.show()

    # ------------------------------------------------------------------
    def _on_reg_changed(self, account_id: str, code: int, reason: str) -> None:
        if code != 0 and not (200 <= code < 300):
            self._last_incident[account_id] = (time.time(), code, reason)
            if self._account is not None and self._account.id == account_id:
                self._refresh_incident(account_id)

    def _on_call_quality(self, call_id: int, mos: float, loss: float,
                         jitter_ms: float, rtt_ms: float) -> None:
        buf = self._quality_buf.setdefault(call_id, deque(maxlen=60))
        buf.append((time.time(), mos, loss, jitter_ms, rtt_ms))
        if self._account is not None:
            self._refresh_stats(self._account)
