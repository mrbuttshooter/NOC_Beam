"""LCD-style call display panel.

The centerpiece of the Calls destination -- a faux LCD screen on a
dark chassis that shows current call state in fixed-width mono. Mimics
the LCD display on a real desktop SIP phone (cf. the Eyebeam classic
shape) but flat, so it stays consistent with the no-gradients style
rule. The cyan inner glow is faked with a layered border instead of
a CSS gradient or QGraphicsEffect blur.

Layout, top to bottom:
  - Status row: STATE (mono uppercase, color-coded) + duration timer
  - Hero row:   the big monospace line (caller URI / dialed number /
                'READY' in idle state)
  - Meta grid:  codec | clock | rtt | mos -- four monospace label/
                value pairs that always show (em-dash when not known)

Four corner LED dots tell the operator at-a-glance state without
having to read a single character:
  - top-left:    REGistration  (green=200/2xx, amber=4xx/auth, red=>=500/timeout)
  - top-right:   TRANSPORT     (cyan=TLS, fg-2=TCP, fg-3=UDP)
  - bottom-left: MEDIA         (green=secure RTP active, amber=plain RTP, off=idle)
  - bottom-right: QUALITY      (green=MOS>=4, amber=3-4, red=<3, off=idle)

Public API:
  - set_idle(account_label, codec)
  - set_dialing(target, codec)
  - set_active(call_label, state, duration_s, codec, rtt_ms, loss_pct, mos)
  - set_incoming(caller_uri)
  - set_registration_led(level)   level in: ok / warn / danger / off
  - set_transport_led(transport)  one of: tls / tcp / udp / off
  - set_media_led(level)          level in: srtp / plain / off
  - set_quality_led(mos)          float; the panel maps to color
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget


# Fixed sizes so the panel always reads as "the screen on a phone"
# rather than reflowing into something amorphous.
PANEL_MIN_W = 520
PANEL_MIN_H = 200
LED_PX = 8
LED_INSET = 12   # distance from corner

LED_COLORS = {
    "ok":     "#66D19E",
    "warn":   "#F0C36D",
    "danger": "#FF5C7A",
    "info":   "#7FD3FF",
    "muted":  "#3B4654",   # off / unknown
    "off":    "#1F252E",
}


def _value_label(text: str = "—") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("LCDValue")
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lbl


def _key_label(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setObjectName("LCDKey")
    return lbl


class LCDPanel(QFrame):
    """Fake LCD-style call status panel.

    The QFrame has a styled border via QSS (#LCDPanel). The four corner
    LEDs are painted in paintEvent so they don't have to be widgets.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LCDPanel")
        self.setMinimumSize(QSize(PANEL_MIN_W, PANEL_MIN_H))
        self.setFrameShape(QFrame.Shape.NoFrame)

        # LED state (color hex for each corner)
        self._led_top_left = LED_COLORS["muted"]
        self._led_top_right = LED_COLORS["muted"]
        self._led_bottom_left = LED_COLORS["muted"]
        self._led_bottom_right = LED_COLORS["muted"]

        # ---- Status row ----------------------------------------------
        self.state_label = QLabel("IDLE")
        self.state_label.setObjectName("LCDState")
        self.duration_label = QLabel("")
        self.duration_label.setObjectName("LCDDuration")

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)
        status_row.addWidget(self.state_label)
        status_row.addStretch(1)
        status_row.addWidget(self.duration_label)

        # ---- Hero row ------------------------------------------------
        self.hero_label = QLabel("READY")
        self.hero_label.setObjectName("LCDHero")
        self.hero_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        # ---- Meta grid (4 key/value cells) ---------------------------
        meta_grid = QGridLayout()
        meta_grid.setContentsMargins(0, 0, 0, 0)
        meta_grid.setHorizontalSpacing(28)
        meta_grid.setVerticalSpacing(2)

        # Two rows per metric: KEY (small caps) + VALUE (mono)
        self.codec_val = _value_label()
        self.clock_val = _value_label()
        self.rtt_val = _value_label()
        self.mos_val = _value_label()
        for col, (key, val) in enumerate((
            ("Codec", self.codec_val),
            ("Clock", self.clock_val),
            ("RTT",   self.rtt_val),
            ("MOS",   self.mos_val),
        )):
            meta_grid.addWidget(_key_label(key), 0, col)
            meta_grid.addWidget(val, 1, col)
        meta_grid.setColumnStretch(4, 1)

        # ---- Compose -------------------------------------------------
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 22, 28, 22)
        outer.setSpacing(14)
        outer.addLayout(status_row)
        outer.addWidget(self.hero_label)
        outer.addStretch(1)
        outer.addLayout(meta_grid)

    # ------------------------------------------------------------------
    # State setters
    # ------------------------------------------------------------------
    def set_idle(self, account_label: str | None, codec: str | None) -> None:
        self.state_label.setText("IDLE")
        self.state_label.setProperty("level", "muted")
        self.duration_label.setText("")
        self.hero_label.setText("READY")
        self.codec_val.setText(codec or "—")
        self.clock_val.setText("—")
        self.rtt_val.setText("—")
        self.mos_val.setText("—")
        self._restyle_state()

    def set_dialing(self, target: str, codec: str | None) -> None:
        self.state_label.setText("CALLING")
        self.state_label.setProperty("level", "info")
        self.duration_label.setText("")
        self.hero_label.setText(target)
        self.codec_val.setText(codec or "—")
        self._restyle_state()

    def set_incoming(self, caller_uri: str) -> None:
        self.state_label.setText("INCOMING")
        self.state_label.setProperty("level", "danger")
        self.duration_label.setText("")
        self.hero_label.setText(caller_uri or "Unknown caller")
        self.codec_val.setText("—")
        self.clock_val.setText("—")
        self.rtt_val.setText("—")
        self.mos_val.setText("—")
        self._restyle_state()

    def set_active(
        self,
        call_label: str,
        state_text: str,
        duration_s: int | float,
        codec: str | None,
        clock_hz: int | None,
        rtt_ms: float | None,
        mos: float | None,
    ) -> None:
        self.state_label.setText(state_text.upper())
        # Map state to LED-style color so the eye picks up status fast.
        if state_text.upper() in ("CONFIRMED", "ACTIVE", "CONNECTED"):
            self.state_label.setProperty("level", "ok")
        elif state_text.upper() == "HELD":
            self.state_label.setProperty("level", "warn")
        elif state_text.upper() in ("DISCONNECTED", "FAILED"):
            self.state_label.setProperty("level", "danger")
        else:
            self.state_label.setProperty("level", "info")
        d = int(max(0, duration_s))
        self.duration_label.setText(f"{d // 60:02d}:{d % 60:02d}")
        self.hero_label.setText(call_label)
        self.codec_val.setText(codec or "—")
        self.clock_val.setText(f"{clock_hz} Hz" if clock_hz else "—")
        self.rtt_val.setText(f"{rtt_ms:.0f} ms" if rtt_ms is not None else "—")
        self.mos_val.setText(f"{mos:.2f}" if mos is not None else "—")
        if mos is not None:
            self.set_quality_led(mos)
        self._restyle_state()

    def _restyle_state(self) -> None:
        # QSS branches via dynamic property -- need an unpolish/polish to apply.
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)

    # ------------------------------------------------------------------
    # LEDs
    # ------------------------------------------------------------------
    def set_registration_led(self, level: str) -> None:
        self._led_top_left = LED_COLORS.get(level, LED_COLORS["muted"])
        self.update()

    def set_transport_led(self, transport: str) -> None:
        color = {
            "tls":  LED_COLORS["info"],
            "tcp":  "#B7C0CC",
            "udp":  "#7C8696",
        }.get(transport.lower() if transport else "", LED_COLORS["muted"])
        self._led_top_right = color
        self.update()

    def set_media_led(self, level: str) -> None:
        color = {
            "srtp":  LED_COLORS["ok"],
            "plain": LED_COLORS["warn"],
            "off":   LED_COLORS["muted"],
        }.get(level, LED_COLORS["muted"])
        self._led_bottom_left = color
        self.update()

    def set_quality_led(self, mos: float | None) -> None:
        if mos is None:
            self._led_bottom_right = LED_COLORS["muted"]
        elif mos >= 4.0:
            self._led_bottom_right = LED_COLORS["ok"]
        elif mos >= 3.0:
            self._led_bottom_right = LED_COLORS["warn"]
        else:
            self._led_bottom_right = LED_COLORS["danger"]
        self.update()

    # ------------------------------------------------------------------
    # Painted LED dots in the four corners.
    # ------------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        w, h = self.width(), self.height()

        for x, y, color_hex in (
            (LED_INSET, LED_INSET, self._led_top_left),
            (w - LED_INSET - LED_PX, LED_INSET, self._led_top_right),
            (LED_INSET, h - LED_INSET - LED_PX, self._led_bottom_left),
            (w - LED_INSET - LED_PX, h - LED_INSET - LED_PX, self._led_bottom_right),
        ):
            color = QColor(color_hex)
            # Soft halo
            halo = QColor(color)
            halo.setAlpha(54)
            painter.setBrush(halo)
            painter.drawEllipse(x - 2, y - 2, LED_PX + 4, LED_PX + 4)
            # Solid dot
            painter.setBrush(color)
            painter.drawEllipse(x, y, LED_PX, LED_PX)
        painter.end()
