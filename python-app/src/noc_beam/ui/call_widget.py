"""Active-call display + in-call controls -- the in-call HERO card.

When a call exists this is the dominant element on the Dial page: a large
mono phone number, a "via <account> · <supplier>" context line, a unified
status chip (Ringing = warn chip + ring timer; Connected = ok chip + talk
timer -- the two are visually and semantically distinct), labeled control
buttons (Mute / Hold / Transfer, icon + text), and a red filled hang-up.

Layout (active call):

    [☎]                            [ Connected ]
              +33 1 23 45 67 89
            via Teles UK · AAA Tel
                 TALK  00:23
      [ Mute ]   [ Hold ]   [ Transfer ]
             [      End call      ]

Layout (incoming):

    [☎]                            [ Incoming ]
              +33 1 23 45 67 89
            via Teles UK · AAA Tel
          [ Reject ]     [ Answer ]

The public API (signals, widget attributes, and the show_*/update_*
methods) is unchanged so phone_shell and the existing tests keep working.
`show_outgoing` / `show_incoming` gained OPTIONAL account/supplier kwargs
(and a `set_context` method) to feed the context line; when omitted the
line simply stays hidden.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.ui.design_tokens import ON_ACCENT, STATUS_DANGER_LIGHT
from noc_beam.ui.rail_icons import rail_icon

# Icon colours. Baked into the SVG pixmap at build time (the app does not
# re-render icons on theme switch), so the neutral/amber glyphs are chosen
# to read on BOTH the light card (#FFFFFF) and the dark card (#2A3346);
# status hues come from design_tokens.
_ICON_NEUTRAL = "#8A93A5"          # mid-grey; reads on light + dark card
_ICON_ON_FILL = ON_ACCENT          # white, on the red hang-up fill
_ICON_MUTED = STATUS_DANGER_LIGHT  # danger fg -- mic is muted
_ICON_HOLD = "#C68A1E"             # amber -- on hold (brightened to read on both)


def _split_peer(remote: str) -> tuple[str, str]:
    """Split a SIP URI / display string into (number-or-uri, friendly-name)."""
    if not remote:
        return ("", "")
    s = remote.strip()
    name = ""
    uri = s
    if s.startswith('"') or s[0:1] == '<':
        if '<' in s and '>' in s:
            name_part, _, rest = s.partition('<')
            uri = rest.rstrip('>')
            name = name_part.strip().strip('"').strip()
    elif '<' in s and '>' in s:
        name_part, _, rest = s.partition('<')
        if name_part.strip():
            name = name_part.strip().strip('"').strip()
        uri = rest.rstrip('>')
    if uri.startswith("sip:"):
        uri = uri[4:]
    elif uri.startswith("sips:"):
        uri = uri[5:]
    user, _, host = uri.partition("@")
    headline = user or uri
    # Operator preference: never use @host as the subtitle fallback.
    # If the peer URI doesn't carry an actual display-name (RFC 3261
    # `"Friendly Name" <sip:foo@bar>` form), the card stays as just the
    # headline. `name` stays empty unless explicitly parsed.
    return (headline.strip(), name.strip())


class CallWidget(QWidget):
    answer_clicked = Signal(int)
    reject_clicked = Signal(int)
    hangup_clicked = Signal(int)
    hold_clicked = Signal(int)
    resume_clicked = Signal(int)
    mute_toggled = Signal(int, bool)
    transfer_clicked = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CallWidget")
        self.setProperty("state", "idle")

        self.call_id = -1
        self._on_hold = False
        self._started_at: float | None = None
        self._connected_at: float | None = None

        # ----- Card (the hero) -------------------------------------------
        self._card = QFrame(self)
        self._card.setObjectName("CallCard")

        # Avatar -- round tint carrying the call's state (styled via QSS).
        self._avatar = QLabel("☎", self._card)
        self._avatar.setObjectName("CallAvatar")
        self._avatar.setFixedSize(32, 32)
        self._avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # State chip (unified chip system): Ringing/warn, Connected/ok, ...
        self.state_label = QLabel("", self._card)
        self.state_label.setObjectName("CallStatePill")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(6)
        header_row.addWidget(self._avatar, 0, Qt.AlignmentFlag.AlignVCenter)
        header_row.addStretch(1)
        header_row.addWidget(self.state_label, 0, Qt.AlignmentFlag.AlignVCenter)

        # Peer headline -- LARGE MONO number (rule 3: numbers -> mono).
        self.peer_label = QLabel("", self._card)
        self.peer_label.setObjectName("CallPeer")
        self.peer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.peer_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.peer_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        # Context line: "via <account> · <supplier>". Populated by
        # set_context() / the show_* kwargs; hidden when no data.
        self.context_label = QLabel("", self._card)
        self.context_label.setObjectName("CallContext")
        self.context_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.context_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.context_label.setVisible(False)

        # Optional display-name sub-line (rarely populated).
        self.peer_sub_label = QLabel("", self._card)
        self.peer_sub_label.setObjectName("CallPeerSub")
        self.peer_sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.peer_sub_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.peer_sub_label.setVisible(False)

        # Timer block: a caption ("RING"/"TALK") that makes ring-time vs
        # talk-time semantically distinct, plus the big mono counter.
        self.timer_caption = QLabel("", self._card)
        self.timer_caption.setObjectName("CallTimerCaption")
        self.timer_caption.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.duration_label = QLabel("", self._card)
        self.duration_label.setObjectName("CallDuration")
        self.duration_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        timer_row = QHBoxLayout()
        timer_row.setContentsMargins(0, 0, 0, 0)
        timer_row.setSpacing(8)
        timer_row.addStretch(1)
        timer_row.addWidget(self.timer_caption, 0, Qt.AlignmentFlag.AlignVCenter)
        timer_row.addWidget(self.duration_label, 0, Qt.AlignmentFlag.AlignVCenter)
        timer_row.addStretch(1)
        self._timer_row_widget = QWidget(self._card)
        self._timer_row_widget.setLayout(timer_row)

        # FAS verdict badge (hidden by default; phone_shell suppresses it).
        from noc_beam.ui.components import FasBadge as _FasBadge
        self.fas_badge = _FasBadge("", self._card)

        # ----- Labeled in-call controls (icon + text) --------------------
        self.mute_btn = self._ctrl_btn("mic", "Mute", "Mute microphone", checkable=True)
        self.hold_btn = self._ctrl_btn("pause", "Hold", "Hold call")
        self.transfer_btn = self._ctrl_btn(
            "call-forward", "Transfer", "Transfer call"
        )

        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.setSpacing(8)
        controls_row.addStretch(1)
        controls_row.addWidget(self.mute_btn)
        controls_row.addWidget(self.hold_btn)
        controls_row.addWidget(self.transfer_btn)
        controls_row.addStretch(1)

        # Red filled hang-up -- full width, dominant end action.
        self.hangup_btn = QPushButton("End call", self._card)
        self.hangup_btn.setObjectName("CallHangupBtn")
        self.hangup_btn.setAccessibleName("End call")
        self.hangup_btn.setIcon(rail_icon("phone-down", color=_ICON_ON_FILL, px=16))
        self.hangup_btn.setIconSize(QSize(16, 16))
        self.hangup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hangup_btn.setMinimumHeight(40)
        self.hangup_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Speaker kept around for the QSS contract / API compatibility but
        # hidden -- speaker mute lives in the audio strip.
        self.speaker_btn = QPushButton("Speaker", self._card)
        self.speaker_btn.setObjectName("CallControlButton")
        self.speaker_btn.setCheckable(True)
        self.speaker_btn.setVisible(False)

        actions_col = QVBoxLayout()
        actions_col.setContentsMargins(0, 0, 0, 0)
        actions_col.setSpacing(8)
        actions_col.addLayout(controls_row)
        actions_col.addWidget(self.hangup_btn)
        self._actions_widget = QWidget(self._card)
        self._actions_widget.setLayout(actions_col)

        # ----- Incoming-call buttons (Reject / Answer) -------------------
        self.answer_btn = QPushButton("Answer", self._card)
        self.answer_btn.setObjectName("CallAnswerBtn")
        self.answer_btn.setAccessibleName("Answer incoming call")
        self.answer_btn.setMinimumHeight(40)
        self.answer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reject_btn = QPushButton("Reject", self._card)
        self.reject_btn.setObjectName("CallRejectBtn")
        self.reject_btn.setAccessibleName("Reject incoming call")
        self.reject_btn.setMinimumHeight(40)
        self.reject_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        incoming_row = QHBoxLayout()
        incoming_row.setContentsMargins(0, 0, 0, 0)
        incoming_row.setSpacing(10)
        incoming_row.addWidget(self.reject_btn, 1)
        incoming_row.addWidget(self.answer_btn, 1)
        self._incoming_widget = QWidget(self._card)
        self._incoming_widget.setLayout(incoming_row)
        self._incoming_widget.setVisible(False)

        # ----- Card layout (vertical hero) -------------------------------
        card_col = QVBoxLayout(self._card)
        card_col.setContentsMargins(16, 12, 16, 14)
        card_col.setSpacing(6)
        card_col.addLayout(header_row)
        card_col.addWidget(self.peer_label)
        card_col.addWidget(self.context_label)
        card_col.addWidget(self.peer_sub_label)
        card_col.addWidget(self._timer_row_widget)
        card_col.addWidget(self.fas_badge, 0, Qt.AlignmentFlag.AlignHCenter)
        card_col.addSpacing(2)
        card_col.addWidget(self._actions_widget)
        card_col.addWidget(self._incoming_widget)

        # Back-compat aliases for callers.
        self.codec_label = self.peer_sub_label
        self.quality_label = QLabel("", self)
        self.quality_label.setVisible(False)

        # ----- Wire signals ----------------------------------------------
        self.answer_btn.clicked.connect(lambda: self.answer_clicked.emit(self.call_id))
        self.reject_btn.clicked.connect(lambda: self.reject_clicked.emit(self.call_id))
        self.hangup_btn.clicked.connect(lambda: self.hangup_clicked.emit(self.call_id))
        self.hold_btn.clicked.connect(self._on_hold_clicked)
        self.mute_btn.toggled.connect(lambda b: self.mute_toggled.emit(self.call_id, b))
        self.mute_btn.toggled.connect(self._set_mute_icon)
        self.transfer_btn.clicked.connect(lambda: self.transfer_clicked.emit(self.call_id))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(0)
        layout.addWidget(self._card)

        # Tick once a second while a call is live to update the timer.
        try:
            self._duration_timer = QTimer(self)
        except TypeError:
            self._duration_timer = QTimer()
        try:
            self._duration_timer.setInterval(1000)
            self._duration_timer.timeout.connect(self._tick_duration)
        except Exception:
            pass

        self.show_idle()

    # ------------------------------------------------------------------
    # Construction helper
    # ------------------------------------------------------------------
    def _ctrl_btn(
        self,
        icon_name: str,
        text: str,
        tooltip: str,
        *,
        checkable: bool = False,
    ) -> QToolButton:
        """A labeled in-call control: icon on top, text label underneath."""
        btn = QToolButton(self._card)
        btn.setObjectName("CallCtrlBtn")
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        btn.setText(text)
        btn.setIcon(rail_icon(icon_name, color=_ICON_NEUTRAL, px=20))
        btn.setIconSize(QSize(20, 20))
        btn.setCheckable(checkable)
        btn.setToolTip(tooltip)
        # Accessible name announces the action verb for screen readers.
        btn.setAccessibleName(tooltip)
        btn.setAccessibleDescription(tooltip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumSize(72, 56)
        btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        return btn

    # ------------------------------------------------------------------
    # Context line
    # ------------------------------------------------------------------
    def set_context(self, account: str = "", supplier: str = "") -> None:
        """Populate the "via <account> · <supplier>" line under the number.

        Either part may be empty; the line hides when both are empty.
        Called by phone_shell (see _select_call) with the CallRecord's
        account_label / supplier_label.
        """
        account = (account or "").strip()
        supplier = (supplier or "").strip()
        parts = [p for p in (account, supplier) if p]
        if parts:
            self.context_label.setText("via " + " · ".join(parts))
            self.context_label.setVisible(True)
        else:
            self.context_label.setText("")
            self.context_label.setVisible(False)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def _reset_mute_btn(self) -> None:
        """Force the Mute button to unchecked without emitting toggled.

        The CallWidget is recycled across calls, so a stale checked state
        would carry a previous call's mute into a fresh one. The CallRecord
        is the source of truth; _select_call syncs the button back when the
        record is actually muted.
        """
        self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(False)
        self.mute_btn.blockSignals(False)
        self._set_mute_icon(False)

    def _set_mute_icon(self, muted: bool) -> None:
        # No dedicated mic-off glyph in the icon set -- signal the muted
        # state with the danger colour + the checked-chip background (QSS)
        # + the label flip to "Unmute".
        self.mute_btn.setIcon(
            rail_icon("mic", color=_ICON_MUTED if muted else _ICON_NEUTRAL, px=20)
        )
        self.mute_btn.setText("Unmute" if muted else "Mute")

    def _set_timer_phase(self, phase: str) -> None:
        """phase in {'ring','talk','hold','idle'} -- drives caption + colour."""
        captions = {"ring": "RING", "talk": "TALK", "hold": "HOLD"}
        cap = captions.get(phase, "")
        self.timer_caption.setText(cap)
        self.timer_caption.setVisible(bool(cap))
        for lbl in (self.duration_label, self.timer_caption):
            lbl.setProperty("phase", phase)
            lbl.style().unpolish(lbl)
            lbl.style().polish(lbl)

    def show_idle(self) -> None:
        self.call_id = -1
        self._set_peer("", "")
        self.set_context("", "")
        try:
            from noc_beam.ui import motion

            motion.stop_pulse(self.state_label)
        except Exception:
            pass
        self.state_label.setText("")
        self.state_label.setVisible(False)
        self.duration_label.setText("")
        self._set_timer_phase("idle")
        self._started_at = None
        self._connected_at = None
        try:
            self._duration_timer.stop()
        except Exception:
            pass
        self._set_state("idle")
        self._set_active_row(True)
        # Reset Mute to unchecked (source of truth is the CallRecord).
        self._reset_mute_btn()
        for b in (self.answer_btn, self.reject_btn, self.hangup_btn,
                  self.hold_btn, self.mute_btn, self.transfer_btn):
            b.setEnabled(False)

    def show_outgoing(
        self,
        call_id: int,
        target: str,
        account: str = "",
        supplier: str = "",
    ) -> None:
        is_new_call = self.call_id != call_id
        self.call_id = call_id
        headline, sub = _split_peer(target)
        self._set_peer(headline or target, sub)
        if account or supplier:
            self.set_context(account, supplier)
        self.state_label.setText("Calling…")
        self.state_label.setProperty("level", "progress")
        self.state_label.setVisible(True)
        self.duration_label.setText("00:00")
        self._set_timer_phase("ring")
        if is_new_call or self._started_at is None:
            self._started_at = time.time()
            self._connected_at = None
        self._ensure_duration_timer_running()
        self._set_active_row(True)
        self.hangup_btn.setEnabled(True)
        self.mute_btn.setEnabled(False)
        self.hold_btn.setEnabled(False)
        self.transfer_btn.setEnabled(False)
        self._set_state("outgoing")
        if is_new_call:
            self._entrance()

    def show_incoming(
        self,
        call_id: int,
        remote: str,
        account: str = "",
        supplier: str = "",
    ) -> None:
        is_new_call = self.call_id != call_id
        self.call_id = call_id
        headline, sub = _split_peer(remote)
        self._set_peer(headline or remote, sub)
        if account or supplier:
            self.set_context(account, supplier)
        self.state_label.setText("Incoming")
        self.state_label.setProperty("level", "progress")
        self.state_label.setVisible(True)
        self.duration_label.setText("")
        self._set_timer_phase("ring")
        self._set_active_row(False)
        self._reset_mute_btn()
        self.answer_btn.setEnabled(True)
        self.reject_btn.setEnabled(True)
        self.hangup_btn.setEnabled(False)
        self.hold_btn.setEnabled(False)
        self.mute_btn.setEnabled(False)
        self._set_state("incoming")
        if is_new_call:
            self._entrance()

    def _entrance(self) -> None:
        """Visual 2.0: hero card entrance -- fade + slide-up into place."""
        try:
            from noc_beam.ui import motion

            motion.slide_fade_in(self, dy=10, duration=motion.DUR_SLOW)
        except Exception:
            pass

    def update_state(self, state_name: str, code: int, reason: str) -> None:
        if state_name == "CALLING":
            pill = "Calling..."
        elif state_name == "EARLY":
            pill = "Ringing"
        elif state_name == "CONFIRMED":
            pill = "Connected"
        elif state_name == "HELD":
            pill = "On hold"
        elif state_name == "INCOMING":
            pill = "Incoming"
        elif code:
            pill = f"{code} {reason}".strip()
        else:
            pill = state_name.title()
        self.state_label.setText(pill)
        # Visual 2.0 motion: the chip breathes while the call is ringing
        # (either direction) and settles the moment it connects or ends.
        try:
            from noc_beam.ui import motion

            if state_name in ("CALLING", "EARLY", "INCOMING"):
                motion.pulse(self.state_label)
            else:
                motion.stop_pulse(self.state_label)
        except Exception:
            pass
        self._on_hold = state_name == "HELD"
        self.hold_btn.setToolTip("Resume call" if self._on_hold else "Hold call")
        self.hold_btn.setText("Resume" if self._on_hold else "Hold")
        # Pause bars while talking; play triangle while held. Amber tint
        # signals "paused" without needing words.
        self.hold_btn.setIcon(
            rail_icon(
                "play" if self._on_hold else "pause",
                color=_ICON_HOLD if self._on_hold else _ICON_NEUTRAL,
                px=20,
            )
        )
        # The state chip stays visible for the whole call now (it is the
        # hero's status indicator), unlike the old compact strip which hid
        # it once connected.
        self.state_label.setVisible(True)

        # Chip level -> unified chip colours (ok/warn/danger/info/muted).
        if state_name == "CONFIRMED":
            self.state_label.setProperty("level", "ok")
        elif state_name == "HELD":
            self.state_label.setProperty("level", "progress")
        elif 200 <= code < 300:
            self.state_label.setProperty("level", "ok")
        elif 100 <= code < 200:
            self.state_label.setProperty("level", "progress")
        elif code in (401, 407):
            self.state_label.setProperty("level", "auth")
        elif 400 <= code < 600:
            self.state_label.setProperty("level", "error")
        else:
            self.state_label.setProperty("level", "progress")
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)

        if state_name == "INCOMING":
            self._set_state("incoming")
            self._set_active_row(False)
        elif state_name in ("CONFIRMED", "HELD"):
            self._set_state("active")
            self._set_active_row(True)
        elif state_name == "DISCONNECTED":
            self._set_state("idle")
        else:
            self._set_state("outgoing")
            self._set_active_row(True)

        in_call = state_name in ("CONFIRMED", "HELD")
        self.hold_btn.setEnabled(in_call)
        self.mute_btn.setEnabled(in_call)
        self.transfer_btn.setEnabled(in_call)
        self.hangup_btn.setEnabled(state_name != "DISCONNECTED")

        # Timer phase: ring-time (amber) until connected, talk-time (green)
        # once confirmed, hold (amber) while held.
        if state_name == "CONFIRMED":
            if self._connected_at is None:
                self._connected_at = time.time()
            self._set_timer_phase("talk")
            self._ensure_duration_timer_running()
            self._tick_duration()
        elif state_name in ("CALLING", "EARLY"):
            if self._started_at is None:
                self._started_at = time.time()
            self._set_timer_phase("ring")
            self._ensure_duration_timer_running()
            self._tick_duration()
        elif state_name == "HELD":
            self._set_timer_phase("hold")
            self._tick_duration()
        elif state_name == "DISCONNECTED":
            try:
                self._duration_timer.stop()
            except Exception:
                pass

    def update_quality(self, mos: float, packet_loss_pct: float) -> None:
        # Audio-quality lives in the audio strip meters + CdrDetailDialog.
        return

    def update_media(self, codec: str, clock: int, channels: int) -> None:
        # Codec is per-account config, not per-call noise.
        return

    def update_fas(self, verdict: str, confidence: float = 0.0, reasons: str = "") -> None:
        """Update the FAS badge for this call. Empty verdict hides it."""
        try:
            self.fas_badge.update_verdict(verdict, confidence, reasons)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _set_peer(self, headline: str, subtitle: str) -> None:
        self.peer_label.setText(headline)
        self.peer_sub_label.setText(subtitle)
        self.peer_sub_label.setVisible(bool(subtitle))

    def _set_active_row(self, active: bool) -> None:
        self._actions_widget.setVisible(active)
        self._incoming_widget.setVisible(not active)

    def _set_state(self, state: str) -> None:
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)
        self._card.setProperty("state", state)
        self._card.style().unpolish(self._card)
        self._card.style().polish(self._card)
        # Polishing the parent does NOT cascade; the avatar bg is selected
        # by QFrame#CallCard[state="X"] QLabel#CallAvatar, so re-polish it.
        self._avatar.style().unpolish(self._avatar)
        self._avatar.style().polish(self._avatar)

    def _on_hold_clicked(self) -> None:
        if self._on_hold:
            self.resume_clicked.emit(self.call_id)
        else:
            self.hold_clicked.emit(self.call_id)

    def _ensure_duration_timer_running(self) -> None:
        try:
            if not self._duration_timer.isActive():
                self._duration_timer.start()
        except Exception:
            pass

    def _tick_duration(self) -> None:
        anchor = self._connected_at if self._connected_at is not None else self._started_at
        if anchor is None:
            self.duration_label.setText("")
            return
        elapsed = int(time.time() - anchor)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        fmt = f"{h:02d}:{m:02d}:{s:02d}" if h else f"00:{m:02d}:{s:02d}"
        self.duration_label.setText(fmt)

    @staticmethod
    def _mos_to_bars(mos: float) -> int:
        if mos < 2.5:
            return 1
        if mos < 3.1:
            return 2
        if mos < 3.6:
            return 3
        return 4
