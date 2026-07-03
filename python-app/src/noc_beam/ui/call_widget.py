"""Active-call display + in-call controls -- single compact row.

Bria-style: the active call is rendered as ONE horizontal strip with
inline icon-buttons, NOT a chunky card with a row of big text buttons
below. Saves ~80 px of vertical real estate so the dial keypad and
recents list stay visible during a call.

Layout (active call):

  [avatar] [peer name]               [00:00:23 · 200 OK]  [M] [H] [T] [End]
           [codec  ·  MOS]

Layout (incoming):

  [avatar] [peer name]                                    [Reject]  [Answer]
           [Incoming call]

The compact row scales to whatever vertical space is available, but
typical height is ~56 px (vs ~140 px for the previous card+button
stack).
"""
from __future__ import annotations

import time

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.ui.rail_icons import rail_icon


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
    # `"Friendly Name" <sip:foo@bar>` form), the call card stays as
    # just the headline -- `iptel.org` etc under the number was
    # judged useless. `name` stays empty unless explicitly parsed.
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

        # ----- Card (single compact row) ---------------------------------
        self._card = QFrame(self)
        self._card.setObjectName("CallCard")

        # Avatar -- small circle with handset glyph.
        self._avatar = QLabel("☎", self._card)
        self._avatar.setObjectName("CallAvatar")
        self._avatar.setFixedSize(28, 28)
        self._avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Peer column (top: peer name; bottom: codec / MOS / state-sub).
        # Both labels get an Ignored horizontal size policy so the
        # layout can shrink the column when the window is narrow --
        # without this the labels enforce their text-width minimum
        # and the actions row gets pushed off the right edge in
        # ~410 px softphone windows.
        from PySide6.QtWidgets import QSizePolicy as _SP
        self.peer_label = QLabel("", self._card)
        self.peer_label.setObjectName("CallPeer")
        self.peer_label.setSizePolicy(_SP.Policy.Ignored, _SP.Policy.Preferred)
        self.peer_sub_label = QLabel("", self._card)
        self.peer_sub_label.setObjectName("CallPeerSub")
        self.peer_sub_label.setSizePolicy(_SP.Policy.Ignored, _SP.Policy.Preferred)
        peer_col = QVBoxLayout()
        peer_col.setContentsMargins(0, 0, 0, 0)
        peer_col.setSpacing(0)
        peer_col.addWidget(self.peer_label)
        peer_col.addWidget(self.peer_sub_label)

        # Right meta: state pill on top, duration below.
        self.state_label = QLabel("", self._card)
        self.state_label.setObjectName("CallStatePill")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.duration_label = QLabel("", self._card)
        self.duration_label.setObjectName("CallDuration")
        self.duration_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # FAS verdict badge. Hidden by default; shown when the FAS
        # engine emits a verdict for this call. Lives in the meta
        # column so it sits next to state + duration without stealing
        # space from the peer column.
        from noc_beam.ui.components import FasBadge as _FasBadge
        self.fas_badge = _FasBadge("", self._card)

        meta_col = QVBoxLayout()
        meta_col.setContentsMargins(0, 0, 0, 0)
        meta_col.setSpacing(0)
        meta_col.addWidget(self.state_label, 0, Qt.AlignmentFlag.AlignRight)
        meta_col.addWidget(self.duration_label)
        meta_col.addWidget(self.fas_badge, 0, Qt.AlignmentFlag.AlignRight)

        # Inline icon-button actions for the active call. Tooltip
        # carries the verb so the icon-only chrome stays terse.
        self.mute_btn = self._icon_btn(
            "mic", "Mute microphone", checkable=True, color="#1F2933"
        )
        # Pause-bars for Hold (universal "pause" glyph). Swaps to a
        # Play triangle when on hold.
        self.hold_btn = self._icon_btn(
            "pause", "Hold call", checkable=False, color="#1F2933"
        )
        # Phone-with-forward-arrow for Transfer.
        self.transfer_btn = self._icon_btn(
            "call-forward", "Transfer call", checkable=False, color="#1F2933"
        )
        self.hangup_btn = self._icon_btn(
            "phone-down", "End call", checkable=False, color="#FFFFFF",
            object_name="CallRowEndBtn",
        )
        # End button: 48 px wide (down from 60). At 60 px the four
        # action buttons + 4 px spacing + avatar + peer + duration
        # column pushed the card past the parent's content width,
        # clipping the End button off the right edge in the
        # ~420 px softphone window. 48 still reads as wider /
        # higher visual rank than the 36 px square Mute/Hold/Transfer
        # while leaving room inside the layout.
        self.hangup_btn.setFixedSize(48, 36)

        # Speaker stays around for the QSS contract / API but is
        # hidden by default in compact mode -- speaker mute already
        # lives on the top strip.
        self.speaker_btn = QPushButton("Speaker", self._card)
        self.speaker_btn.setObjectName("CallControlButton")
        self.speaker_btn.setCheckable(True)
        self.speaker_btn.setVisible(False)

        actions_row = QHBoxLayout()
        actions_row.setContentsMargins(0, 0, 0, 0)
        # 2 px between buttons (was 4) -- saves 6 px across the row,
        # which combined with the 48 px end button + shrinkable peer
        # column keeps the End button on-screen even when there's a
        # multi-call strip eating vertical real estate above.
        actions_row.setSpacing(2)
        actions_row.addWidget(self.mute_btn)
        actions_row.addWidget(self.hold_btn)
        actions_row.addWidget(self.transfer_btn)
        actions_row.addWidget(self.hangup_btn)

        # ----- Incoming-call buttons (Reject / Answer) -------------------
        self.answer_btn = QPushButton("Answer", self._card)
        self.answer_btn.setObjectName("CallButton")
        self.answer_btn.setAccessibleName("Answer incoming call")
        self.answer_btn.setMinimumHeight(28)
        self.reject_btn = QPushButton("Reject", self._card)
        self.reject_btn.setObjectName("RejectButton")
        self.reject_btn.setAccessibleName("Reject incoming call")
        self.reject_btn.setMinimumHeight(28)

        incoming_row = QHBoxLayout()
        incoming_row.setContentsMargins(0, 0, 0, 0)
        incoming_row.setSpacing(6)
        incoming_row.addWidget(self.reject_btn)
        incoming_row.addWidget(self.answer_btn)

        self._actions_widget = QWidget(self._card)
        self._actions_widget.setLayout(actions_row)
        self._incoming_widget = QWidget(self._card)
        self._incoming_widget.setLayout(incoming_row)
        self._incoming_widget.setVisible(False)

        # ----- Card layout (one horizontal row) --------------------------
        card_row = QHBoxLayout(self._card)
        card_row.setContentsMargins(6, 4, 4, 4)
        # Tightened spacing (8 -> 6) to claw back another 8 px across
        # the row so the End button fits even in compact-window mode.
        card_row.setSpacing(6)
        card_row.addWidget(self._avatar, 0, Qt.AlignmentFlag.AlignVCenter)
        card_row.addLayout(peer_col, 1)
        card_row.addLayout(meta_col, 0)
        card_row.addWidget(self._actions_widget, 0, Qt.AlignmentFlag.AlignVCenter)
        card_row.addWidget(self._incoming_widget, 0, Qt.AlignmentFlag.AlignVCenter)

        # Codec + quality kept as a stash on the peer-sub line so the
        # card stays single-row (instead of a separate meta row).
        self.codec_label = self.peer_sub_label  # alias for compatibility
        self.quality_label = QLabel("", self)   # kept for callers
        self.quality_label.setVisible(False)

        # ----- Wire signals ----------------------------------------------
        self.answer_btn.clicked.connect(lambda: self.answer_clicked.emit(self.call_id))
        self.reject_btn.clicked.connect(lambda: self.reject_clicked.emit(self.call_id))
        self.hangup_btn.clicked.connect(lambda: self.hangup_clicked.emit(self.call_id))
        self.hold_btn.clicked.connect(self._on_hold_clicked)
        self.mute_btn.toggled.connect(lambda b: self.mute_toggled.emit(self.call_id, b))
        self.transfer_btn.clicked.connect(lambda: self.transfer_clicked.emit(self.call_id))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(0)
        layout.addWidget(self._card)

        # Tick once a second while connected to update duration.
        # Parented when possible (proper Qt ownership), falls back to
        # parentless for the unit-test FakeTimer.
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
    def _icon_btn(
        self,
        icon_name: str,
        tooltip: str,
        *,
        checkable: bool = False,
        color: str = "#1F2933",
        object_name: str = "CallRowIconBtn",
    ) -> QToolButton:
        btn = QToolButton(self._card)
        btn.setObjectName(object_name)
        # 18 px icon in a 36 px hit-target -- matches Bria's call-card
        # control row. Was 14 / 28 which read as toolbar chrome, not
        # call controls, and failed the WCAG 2.5.5 minimum 24x24 hit
        # area for the End/Hold/Transfer buttons.
        btn.setIcon(rail_icon(icon_name, color=color, px=18))
        btn.setIconSize(QSize(18, 18))
        btn.setCheckable(checkable)
        btn.setToolTip(tooltip)
        # Accessible name mirrors the tooltip so screen-readers
        # announce the action verb (Mute microphone / End call /
        # Hold call) instead of just "tool button". A11y sweep.
        btn.setAccessibleName(tooltip)
        btn.setAccessibleDescription(tooltip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(36, 36)
        return btn

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def _reset_mute_btn(self) -> None:
        """Force the Mute button to unchecked without emitting toggled.

        The CallWidget is recycled across calls, so a stale checked
        state would carry a previous call's mute into a fresh one. The
        CallRecord is the source of truth; PhoneShell._select_call syncs
        the button back to checked when the record is actually muted.
        """
        self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(False)
        self.mute_btn.blockSignals(False)

    def show_idle(self) -> None:
        self.call_id = -1
        self._set_peer("", "")
        self.state_label.setText("")
        self.duration_label.setText("")
        self._started_at = None
        self._connected_at = None
        try:
            self._duration_timer.stop()
        except Exception:
            pass
        self._set_state("idle")
        self._set_active_row(True)
        # Reset the Mute button to unchecked -- otherwise a card recycled
        # from a previously-muted call would show "muted" for the next
        # call while the CallRecord (the source of truth) says unmuted,
        # desyncing the whole mute graph. blockSignals so this reset
        # doesn't emit mute_toggled and flip the real mic state.
        self._reset_mute_btn()
        for b in (self.answer_btn, self.reject_btn, self.hangup_btn,
                  self.hold_btn, self.mute_btn, self.transfer_btn):
            b.setEnabled(False)

    def show_outgoing(self, call_id: int, target: str) -> None:
        is_new_call = self.call_id != call_id
        self.call_id = call_id
        headline, sub = _split_peer(target)
        # Don't fall back to "Outgoing call" -- the state label
        # ("Calling.../In call/Ended") already carries that signal.
        # The peer subtitle is now only used when the URI carries a
        # real display-name; otherwise it stays empty and hidden.
        self._set_peer(headline or target, sub)
        self.state_label.setText("Calling…")
        self.state_label.setProperty("level", "progress")
        self.duration_label.setText("00:00")
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

    def show_incoming(self, call_id: int, remote: str) -> None:
        self.call_id = call_id
        headline, sub = _split_peer(remote)
        # Same rationale as show_outgoing -- subtitle only carries a
        # real display-name; state label handles RINGING/Calling/etc.
        self._set_peer(headline or remote, sub)
        self.state_label.setText("RINGING")
        self.state_label.setProperty("level", "progress")
        self.duration_label.setText("")
        self._set_active_row(False)
        # Fresh incoming call -> start from an unmuted button (record is
        # the source of truth; _select_call re-syncs if actually muted).
        self._reset_mute_btn()
        self.answer_btn.setEnabled(True)
        self.reject_btn.setEnabled(True)
        self.hangup_btn.setEnabled(False)
        self.hold_btn.setEnabled(False)
        self.mute_btn.setEnabled(False)
        self._set_state("incoming")

    def update_state(self, state_name: str, code: int, reason: str) -> None:
        if state_name == "CALLING":
            pill = "Calling..."
        elif state_name == "EARLY":
            pill = "Ringing"
        elif code:
            pill = f"{code} {reason}".strip()
        else:
            pill = state_name.title()
        self.state_label.setText(pill)
        self._on_hold = state_name == "HELD"
        self.hold_btn.setToolTip("Resume call" if self._on_hold else "Hold call")
        # Pause bars while talking; play triangle while held. Amber
        # tint signals "this call is paused" without needing words.
        # Match the 18 px icon size the button was built with (line 245).
        # Was 14 px which shrunk the hold/play glyph inside the 18 px
        # slot after the first toggle -- visibly smaller than mute/transfer.
        self.hold_btn.setIcon(
            rail_icon(
                "play" if self._on_hold else "pause",
                color="#E08A1A" if self._on_hold else "#1F2933",
                px=18,
            )
        )
        # Bria-style: hide the SIP status pill once the call is
        # CONFIRMED -- only show it for outgoing/incoming/error states
        # where the user actually needs the code.
        if state_name in ("CONFIRMED", "HELD"):
            self.state_label.setVisible(False)
        else:
            self.state_label.setVisible(True)

        if 200 <= code < 300:
            self.state_label.setProperty("level", "ok")
        elif 100 <= code < 200:
            self.state_label.setProperty("level", "progress")
        elif code in (401, 407):
            self.state_label.setProperty("level", "auth")
        elif 400 <= code < 600:
            self.state_label.setProperty("level", "error")
        else:
            self.state_label.setProperty("level", "muted")
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

        if state_name == "CONFIRMED":
            if self._connected_at is None:
                self._connected_at = time.time()
            self._ensure_duration_timer_running()
            self._tick_duration()
        elif state_name in ("CALLING", "EARLY"):
            if self._started_at is None:
                self._started_at = time.time()
            self._ensure_duration_timer_running()
            self._tick_duration()
        elif state_name == "HELD":
            self._tick_duration()
        elif state_name == "DISCONNECTED":
            try:
                self._duration_timer.stop()
            except Exception:
                pass

    def update_quality(self, mos: float, packet_loss_pct: float) -> None:
        # Operator dropped the codec @ rate line per "useless info"
        # feedback. MOS / packet-loss also dropped from the call card
        # -- the live audio meters + RX/TX bars in the top strip
        # already cover audio-quality at-a-glance, and full numeric
        # detail is one click away in CdrDetailDialog.
        return

    def update_media(self, codec: str, clock: int, channels: int) -> None:
        # Codec display removed (was rendered as "PCMA @ 8000 Hz"
        # under the dialled number) -- the operator flagged it as
        # noise; the codec is set per-account in Settings, so
        # showing it on every active call adds nothing.
        return

    def update_fas(self, verdict: str, confidence: float = 0.0, reasons: str = "") -> None:
        """Update the FAS badge for this call.

        Called by phone_shell when the call's CallRecord re-renders with
        an updated fas_verdict. Empty verdict hides the badge.
        """
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
        # Polishing the parent does NOT cascade to children. The avatar
        # background is selected by `QFrame#CallCard[state="X"] QLabel
        # #CallAvatar`, so the child label needs its own re-polish for
        # the new state's bg colour to apply.
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
