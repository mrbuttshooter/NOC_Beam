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
    if not name:
        name = host or ""
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
        self.peer_label = QLabel("", self._card)
        self.peer_label.setObjectName("CallPeer")
        self.peer_sub_label = QLabel("", self._card)
        self.peer_sub_label.setObjectName("CallPeerSub")
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
        meta_col = QVBoxLayout()
        meta_col.setContentsMargins(0, 0, 0, 0)
        meta_col.setSpacing(0)
        meta_col.addWidget(self.state_label, 0, Qt.AlignmentFlag.AlignRight)
        meta_col.addWidget(self.duration_label)

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
        actions_row.setSpacing(4)
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
        card_row.setContentsMargins(8, 4, 6, 4)
        card_row.setSpacing(8)
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
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(36, 36)
        return btn

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def show_idle(self) -> None:
        self.call_id = -1
        self._set_peer("", "")
        self.state_label.setText("")
        self.duration_label.setText("")
        self._connected_at = None
        try:
            self._duration_timer.stop()
        except Exception:
            pass
        self._set_state("idle")
        self._set_active_row(True)
        for b in (self.answer_btn, self.reject_btn, self.hangup_btn,
                  self.hold_btn, self.mute_btn, self.transfer_btn):
            b.setEnabled(False)

    def show_outgoing(self, call_id: int, target: str) -> None:
        self.call_id = call_id
        headline, sub = _split_peer(target)
        self._set_peer(headline or target, sub or "Outgoing call")
        self.state_label.setText("Calling…")
        self.state_label.setProperty("level", "progress")
        self.duration_label.setText("00:00")
        self._set_active_row(True)
        self.hangup_btn.setEnabled(True)
        self.mute_btn.setEnabled(False)
        self.hold_btn.setEnabled(False)
        self.transfer_btn.setEnabled(False)
        self._set_state("outgoing")

    def show_incoming(self, call_id: int, remote: str) -> None:
        self.call_id = call_id
        headline, sub = _split_peer(remote)
        self._set_peer(headline or remote, sub or "Incoming call")
        self.state_label.setText("RINGING")
        self.state_label.setProperty("level", "progress")
        self.duration_label.setText("")
        self._set_active_row(False)
        self.answer_btn.setEnabled(True)
        self.reject_btn.setEnabled(True)
        self.hangup_btn.setEnabled(False)
        self.hold_btn.setEnabled(False)
        self.mute_btn.setEnabled(False)
        self._set_state("incoming")

    def update_state(self, state_name: str, code: int, reason: str) -> None:
        if code:
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
            try:
                if not self._duration_timer.isActive():
                    self._duration_timer.start()
            except Exception:
                pass
            self._tick_duration()
        elif state_name == "HELD":
            self._tick_duration()
        elif state_name == "DISCONNECTED":
            try:
                self._duration_timer.stop()
            except Exception:
                pass

    def update_quality(self, mos: float, packet_loss_pct: float) -> None:
        # MOS shown inline in the peer-sub line, not a separate row.
        # Keep the existing peer-sub text (codec) and append MOS.
        existing = self.peer_sub_label.text()
        # Strip any prior MOS suffix.
        base = existing.split("  ·  MOS", 1)[0]
        loss = f", loss {packet_loss_pct:.1f}%" if packet_loss_pct > 0 else ""
        self.peer_sub_label.setText(f"{base}  ·  MOS {mos:.1f}{loss}")

    def update_media(self, codec: str, clock: int, channels: int) -> None:
        if not codec:
            return
        chan = f", {channels}ch" if channels and channels > 1 else ""
        self.peer_sub_label.setText(f"{codec} @ {clock} Hz{chan}")

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

    def _tick_duration(self) -> None:
        if self._connected_at is None:
            self.duration_label.setText("")
            return
        elapsed = int(time.time() - self._connected_at)
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
