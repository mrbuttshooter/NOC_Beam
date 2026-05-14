"""Active-call display + in-call controls."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
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
    mute_toggled = Signal(int, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.call_id = -1

        self.peer_label = QLabel("Idle")
        self.peer_label.setObjectName("CallPeer")
        self.state_label = QLabel("")
        self.state_label.setObjectName("CallState")
        self.codec_label = QLabel("")
        self.codec_label.setObjectName("CallCodec")

        self.answer_btn = QPushButton("Answer")
        self.reject_btn = QPushButton("Reject")
        self.hangup_btn = QPushButton("Hang up")
        self.hold_btn = QPushButton("Hold")
        self.mute_btn = QPushButton("Mute")
        self.mute_btn.setCheckable(True)

        self.answer_btn.clicked.connect(lambda: self.answer_clicked.emit(self.call_id))
        self.reject_btn.clicked.connect(lambda: self.reject_clicked.emit(self.call_id))
        self.hangup_btn.clicked.connect(lambda: self.hangup_clicked.emit(self.call_id))
        self.hold_btn.clicked.connect(lambda: self.hold_clicked.emit(self.call_id))
        self.mute_btn.toggled.connect(lambda b: self.mute_toggled.emit(self.call_id, b))

        btns = QHBoxLayout()
        for b in (self.answer_btn, self.reject_btn, self.hangup_btn, self.hold_btn, self.mute_btn):
            btns.addWidget(b)

        layout = QVBoxLayout(self)
        layout.addWidget(self.peer_label)
        layout.addWidget(self.state_label)
        layout.addWidget(self.codec_label)
        layout.addLayout(btns)

        self.show_idle()

    def show_idle(self) -> None:
        self.call_id = -1
        self.peer_label.setText("Idle")
        self.state_label.setText("")
        self.codec_label.setText("")
        for b in (self.answer_btn, self.reject_btn, self.hangup_btn, self.hold_btn, self.mute_btn):
            b.setEnabled(False)

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

    def show_incoming(self, call_id: int, remote: str) -> None:
        self.call_id = call_id
        self.peer_label.setText(f"← {remote}")
        self.state_label.setText("Incoming")
        self.codec_label.setText("")
        self.answer_btn.setEnabled(True)
        self.reject_btn.setEnabled(True)
        self.hangup_btn.setEnabled(True)
        self.hold_btn.setEnabled(False)
        self.mute_btn.setEnabled(False)

    def update_state(self, state_name: str, code: int, reason: str) -> None:
        suffix = f" ({code} {reason})" if code else ""
        self.state_label.setText(f"{state_name}{suffix}")
        in_call = state_name in ("CONFIRMED", "EARLY", "CONNECTING")
        self.hold_btn.setEnabled(state_name == "CONFIRMED")
        self.mute_btn.setEnabled(state_name == "CONFIRMED")
        self.hangup_btn.setEnabled(state_name != "DISCONNECTED")
        if not in_call and state_name == "DISCONNECTED":
            # Caller will switch to idle after this
            pass

    def update_media(self, codec: str, clock: int, channels: int) -> None:
        if codec:
            chan = f", {channels}ch" if channels and channels > 1 else ""
            self.codec_label.setText(f"Codec: {codec} @ {clock} Hz{chan}")
