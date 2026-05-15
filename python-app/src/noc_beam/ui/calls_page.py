"""Calls destination -- LCD panel + dialpad + state-driven bottom section.

Composition (Direction α: LCD + Console):

  +---------------------------------------------------------------+
  |  LCDPanel  --  always visible, always reads current state     |
  |  cyan-glow chassis with REGISTRATION / TRANSPORT / MEDIA /    |
  |  QUALITY LEDs in the four corners                             |
  +---------------------------------------------------------------+
  |                                                               |
  |  Dialpad        |   Bottom section (state-driven)            |
  |  (always there, |   - IDLE  : "Press a key to dial" hint     |
  |   for DTMF      |   - ACTIVE: CallWidget action row          |
  |   while in a    |   - MULTI : call list + CallWidget         |
  |   call)         |                                             |
  +---------------------------------------------------------------+

Outside drives state via set_state(IDLE/ACTIVE/MULTI). MainWindow keeps
calling _sync_calls_state from the call manager events, so the page
stays in sync without any new wiring.
"""
from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from noc_beam.ui.lcd_panel import LCDPanel


# State indices -- public so MainWindow can read them as constants.
IDLE = 0
ACTIVE = 1
MULTI = 2


class CallsPage(QWidget):
    """Always-on LCD on top, dialpad + state-swapped bottom section."""

    def __init__(
        self,
        dialpad: QWidget,
        call_list: QWidget,
        call_widget: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._dialpad = dialpad
        self._call_list = call_list
        self._call_widget = call_widget
        self._meta_provider: Callable[[], tuple[str | None, str | None]] | None = None

        # The constant centerpiece.
        self.lcd = LCDPanel(self)
        self.lcd.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        # Active-call duration timer -- ticks every second while ACTIVE
        # so the LCD's MM:SS counter stays live without MainWindow having
        # to push updates on every tick.
        self._active_started_at: float | None = None
        self._active_label: str = ""
        self._active_codec: str | None = None
        self._active_clock: int | None = None
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._tick_duration)

        # Dialpad holder -- shared furniture, lives in the bottom row.
        self._dialpad_holder = QWidget(self)
        dl = QVBoxLayout(self._dialpad_holder)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(0)
        dl.addWidget(self._dialpad)
        dl.addStretch(1)
        self._dialpad_holder.setMaximumWidth(320)

        # Bottom-section pages.
        self._idle_section = self._build_idle_section()
        self._active_section = self._build_active_section()
        # Multi reuses active for now (Tier-3 wires real callstrips).
        self._stack = QStackedLayout()
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self._idle_section)
        self._stack.addWidget(self._active_section)
        self._stack.addWidget(QWidget())  # placeholder for MULTI

        # Compose: LCD on top, dialpad + state-section below.
        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.setSpacing(20)
        bottom_row.addWidget(self._dialpad_holder)
        bottom_wrap = QWidget(self)
        bottom_wrap_l = QVBoxLayout(bottom_wrap)
        bottom_wrap_l.setContentsMargins(0, 0, 0, 0)
        bottom_wrap_l.addLayout(self._stack)
        bottom_row.addWidget(bottom_wrap, 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(20)
        outer.addWidget(self.lcd)
        outer.addLayout(bottom_row, 1)

        self._state = IDLE

    # ------------------------------------------------------------------
    def _build_idle_section(self) -> QWidget:
        """When idle, the bottom section is a quiet hint about the dialpad."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)
        title = QLabel("Press a key, paste a URI, or use the dialpad")
        title.setObjectName("CallsHint")
        sub = QLabel("Ctrl+K focuses the dial bar. Enter places the call.")
        sub.setObjectName("CallsHintSub")
        layout.addWidget(title)
        layout.addWidget(sub)
        layout.addStretch(1)
        return page

    def _build_active_section(self) -> QWidget:
        """During a call -- show the action row from the existing CallWidget."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._call_list)
        layout.addWidget(self._call_widget, 1)
        return page

    # ------------------------------------------------------------------
    def set_meta_provider(
        self, provider: Callable[[], tuple[str | None, str | None]]
    ) -> None:
        """MainWindow supplies a callback returning (account_label, codec)
        for the idle LCD readout."""
        self._meta_provider = provider

    def set_state(self, state: int) -> None:
        if state not in (IDLE, ACTIVE, MULTI):
            return
        self._state = state
        if state == IDLE:
            self._dialpad_holder.show()
            self._tick_timer.stop()
            self._active_started_at = None
            acc, codec = (self._meta_provider() if self._meta_provider else (None, None))
            self.lcd.set_idle(acc, codec)
            # Best-effort transport LED inferred from typical NOC config: TLS
            # is the recommended default; stay muted until we actually know.
            self.lcd.set_transport_led("")
            self.lcd.set_media_led("off")
            self.lcd.set_quality_led(None)
        else:
            self._dialpad_holder.show()  # keep dialpad accessible for DTMF
        self._stack.setCurrentIndex(state)

    @property
    def state(self) -> int:
        return self._state

    # ------------------------------------------------------------------
    # Active-call LCD updates -- MainWindow calls these as state changes.
    # ------------------------------------------------------------------
    def lcd_show_dialing(self, target: str, codec: str | None) -> None:
        self._active_label = target
        self._active_codec = codec
        self._active_started_at = None
        self._tick_timer.stop()
        self.lcd.set_dialing(target, codec)

    def lcd_show_incoming(self, caller_uri: str) -> None:
        self._active_label = caller_uri
        self._active_started_at = None
        self._tick_timer.stop()
        self.lcd.set_incoming(caller_uri)

    def lcd_show_active(
        self,
        call_label: str,
        state_text: str,
        connected_at: float | None,
        codec: str | None,
        clock_hz: int | None,
        rtt_ms: float | None,
        mos: float | None,
    ) -> None:
        self._active_label = call_label
        self._active_codec = codec
        self._active_clock = clock_hz
        self._active_started_at = connected_at
        if connected_at is not None:
            duration = max(0, time.time() - connected_at)
            if not self._tick_timer.isActive():
                self._tick_timer.start()
        else:
            duration = 0
            self._tick_timer.stop()
        self.lcd.set_active(call_label, state_text, duration, codec, clock_hz, rtt_ms, mos)

    def lcd_update_quality(self, mos: float, loss_pct: float, rtt_ms: float) -> None:
        # Live RTT/MOS refresh while a call is up; rebuild the active row.
        if self._active_started_at is None:
            return
        duration = max(0, time.time() - self._active_started_at)
        self.lcd.set_active(
            self._active_label,
            "ACTIVE",
            duration,
            self._active_codec,
            self._active_clock,
            rtt_ms,
            mos,
        )

    def _tick_duration(self) -> None:
        if self._active_started_at is None:
            return
        duration = int(max(0, time.time() - self._active_started_at))
        self.lcd.duration_label.setText(f"{duration // 60:02d}:{duration % 60:02d}")
