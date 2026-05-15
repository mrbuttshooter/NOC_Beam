"""Active-call display + in-call controls."""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


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

        self.peer_label = QLabel("Idle")
        self.peer_label.setObjectName("CallPeer")
        self.state_label = QLabel("")
        self.state_label.setObjectName("CallState")
        self.codec_label = QLabel("")
        self.codec_label.setObjectName("CallCodec")
        self.duration_label = QLabel("")
        self.duration_label.setObjectName("CallDuration")
        self.quality_label = QLabel("")
        self.quality_label.setObjectName("CallQuality")

        # Tick once a second while connected to update the duration counter.
        self._connected_at: float | None = None
        self._duration_timer = QTimer(self)
        self._duration_timer.setInterval(1000)
        self._duration_timer.timeout.connect(self._tick_duration)

        # Semantic colour hierarchy: Answer is green (CallButton style),
        # Reject and Hang up are red (HangupButton style). Hold/Mute/
        # Transfer are neutral. A panicked user sees the right colour
        # immediately -- critical for the rare-but-stressful incoming
        # call surface.
        self.answer_btn = QPushButton("Answer")
        self.answer_btn.setObjectName("CallButton")
        self.reject_btn = QPushButton("Reject")
        self.reject_btn.setObjectName("HangupButton")
        self.hangup_btn = QPushButton("Hang up")
        self.hangup_btn.setObjectName("HangupButton")
        self.hold_btn = QPushButton("Hold")
        self.hold_btn.setObjectName("CallControlButton")
        self.mute_btn = QPushButton("Mute")
        self.mute_btn.setObjectName("CallControlButton")
        self.mute_btn.setCheckable(True)
        self.transfer_btn = QPushButton("Transfer")
        self.transfer_btn.setObjectName("CallControlButton")
        self.hangup_btn.setAccessibleName("Hang up active call")
        self.hold_btn.setAccessibleName("Hold active call")
        self.mute_btn.setAccessibleName("Mute microphone")
        self.transfer_btn.setAccessibleName("Transfer active call")
        for _b in (self.answer_btn, self.reject_btn, self.hangup_btn,
                   self.hold_btn, self.mute_btn, self.transfer_btn):
            _b.setMinimumWidth(72)
            _b.setMinimumHeight(36)

        self.answer_btn.clicked.connect(lambda: self.answer_clicked.emit(self.call_id))
        self.reject_btn.clicked.connect(lambda: self.reject_clicked.emit(self.call_id))
        self.hangup_btn.clicked.connect(lambda: self.hangup_clicked.emit(self.call_id))
        self.hold_btn.clicked.connect(self._on_hold_clicked)
        self.mute_btn.toggled.connect(lambda b: self.mute_toggled.emit(self.call_id, b))
        self.transfer_btn.clicked.connect(lambda: self.transfer_clicked.emit(self.call_id))

        # 3x2 grid: top row is call lifecycle (Answer / Reject / Hang up),
        # bottom row is in-call (Hold / Mute / Transfer). Fits the narrow
        # phone shell without clipping the labels.
        btns = QGridLayout()
        btns.setHorizontalSpacing(4)
        btns.setVerticalSpacing(4)
        btns.addWidget(self.answer_btn,   0, 0)
        btns.addWidget(self.reject_btn,   0, 1)
        btns.addWidget(self.hangup_btn,   0, 2)
        btns.addWidget(self.hold_btn,     1, 0)
        btns.addWidget(self.mute_btn,     1, 1)
        btns.addWidget(self.transfer_btn, 1, 2)

        meta = QHBoxLayout()
        meta.addWidget(self.codec_label)
        meta.addWidget(self.duration_label)
        meta.addWidget(self.quality_label)
        meta.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.peer_label)
        layout.addWidget(self.state_label)
        layout.addLayout(meta)
        layout.addLayout(btns)
        layout.addStretch(0)

        self.show_idle()

    def show_idle(self) -> None:
        self.call_id = -1
        self.peer_label.setText("Idle")
        self.state_label.setText("")
        self.codec_label.setText("")
        self.duration_label.setText("")
        self.quality_label.setText("")
        self._connected_at = None
        self._duration_timer.stop()
        for b in (self.answer_btn, self.reject_btn, self.hangup_btn,
                  self.hold_btn, self.mute_btn, self.transfer_btn):
            b.setEnabled(False)
        self._set_state("idle")

    def _set_state(self, state: str) -> None:
        """Toggle the dynamic `state` property and re-polish so QSS
        attribute selectors update without restart."""
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)

    def show_outgoing(self, call_id: int, target: str) -> None:
        self.call_id = call_id
        self.peer_label.setText(f"→ {target}")
        self.state_label.setText("Calling…")
        self.codec_label.setText("")
        self.answer_btn.setEnabled(False)
        self.reject_btn.setEnabled(False)
        self.hangup_btn.setEnabled(True)
        self.hold_btn.setEnabled(False)
        self.mute_btn.setEnabled(False)
        self._set_state("outgoing")

    def show_incoming(self, call_id: int, remote: str) -> None:
        self.call_id = call_id
        self.peer_label.setText(f"← {remote}")
        self.state_label.setText("INCOMING CALL")
        self.codec_label.setText("")
        self.answer_btn.setEnabled(True)
        self.reject_btn.setEnabled(True)
        self.hangup_btn.setEnabled(True)
        self.hold_btn.setEnabled(False)
        self.mute_btn.setEnabled(False)
        # Visual urgency: tag the widget so QSS can paint a coloured
        # border / background and bump the peer label size for incoming.
        self._set_state("incoming")

    def update_state(self, state_name: str, code: int, reason: str) -> None:
        suffix = f" ({code} {reason})" if code else ""
        self.state_label.setText(f"{state_name}{suffix}")
        self._on_hold = state_name == "HELD"
        self.hold_btn.setText("Resume" if self._on_hold else "Hold")
        # Drive the urgency banner state so QSS can stop the incoming
        # treatment as soon as we move past INCOMING.
        if state_name == "INCOMING":
            self._set_state("incoming")
        elif state_name in ("CONFIRMED", "HELD"):
            self._set_state("active")
        elif state_name == "DISCONNECTED":
            self._set_state("idle")
        else:
            self._set_state("outgoing")
        in_call = state_name in ("CONFIRMED", "HELD")
        # Hold/resume only valid in CONFIRMED or HELD.
        self.hold_btn.setEnabled(in_call)
        self.mute_btn.setEnabled(in_call)
        # Transfer is only legal once the dialog is established.
        self.transfer_btn.setEnabled(in_call)
        self.hangup_btn.setEnabled(state_name != "DISCONNECTED")

        # Drive the duration timer off the call state.
        if state_name == "CONFIRMED":
            if self._connected_at is None:
                self._connected_at = time.time()
            if not self._duration_timer.isActive():
                self._duration_timer.start()
            self._tick_duration()
        elif state_name == "HELD":
            # Keep the counter running but don't restart it.
            self._tick_duration()
        elif state_name == "DISCONNECTED":
            self._duration_timer.stop()

    def update_quality(self, mos: float, packet_loss_pct: float) -> None:
        """Set the in-call quality indicator. mos in [1.0, 4.5]."""
        bars = self._mos_to_bars(mos)
        glyph = "▮" * bars + "▯" * (4 - bars)
        loss = f"  ⌀ {packet_loss_pct:.1f}%" if packet_loss_pct > 0 else ""
        self.quality_label.setText(f"{glyph}  MOS {mos:.1f}{loss}")

    @staticmethod
    def _mos_to_bars(mos: float) -> int:
        # E-model R-factor → MOS rough buckets: <2.5 dire, <3.1 poor,
        # <3.6 fair, <4.0 good, ≥4.0 excellent.
        if mos < 2.5:
            return 1
        if mos < 3.1:
            return 2
        if mos < 3.6:
            return 3
        return 4

    def _tick_duration(self) -> None:
        if self._connected_at is None:
            self.duration_label.setText("")
            return
        elapsed = int(time.time() - self._connected_at)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        fmt = f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
        self.duration_label.setText(fmt)

    def _on_hold_clicked(self) -> None:
        if self._on_hold:
            self.resume_clicked.emit(self.call_id)
        else:
            self.hold_clicked.emit(self.call_id)

    def update_media(self, codec: str, clock: int, channels: int) -> None:
        if codec:
            chan = f", {channels}ch" if channels and channels > 1 else ""
            self.codec_label.setText(f"Codec: {codec} @ {clock} Hz{chan}")
