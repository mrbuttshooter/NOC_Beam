"""Audio strip widget -- mic / speaker icons + per-direction mute + volumes.

Layout:

  [Mic mute]▾  [Spk mute]▾  [Mic vol]  [Out vol]   ........  TX [---] RX [---]

* The mic icon is a CHECKABLE mute toggle for the microphone (a
  separate small chevron button next to it opens the input-device
  picker -- the icon itself is no longer a popup).
* The speaker icon is a CHECKABLE mute toggle for the output, with a
  matching chevron for the output-device picker.
* Mic volume and output volume each have their own % button that pops
  a slider. They drive `mic_volume_changed` / `volume_changed` signals.
* TX / RX live audio meters are driven by `set_tx_level` / `set_rx_level`
  from a phone-shell QTimer that polls the active call's audio media.

Public API:
    audio = AudioStrip()
    audio.set_input_devices(list[(id, label)])
    audio.set_output_devices(list[(id, label)])
    audio.set_volume(0..100)            # output
    audio.set_mic_volume(0..100)
    audio.set_muted(bool)               # output
    audio.set_mic_muted(bool)
    audio.set_tx_level(0..100)
    audio.set_rx_level(0..100)
    Signals: volume_changed(int), muted_changed(bool),
             mic_volume_changed(int), mic_muted_changed(bool),
             input_device_picked(id), output_device_picked(id)
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.ui.rail_icons import rail_icon


def _icon(name: str, color: str = "#57606A", px: int = 18) -> QIcon:
    """Reuse the rail-icon SVG renderer for the audio strip glyphs."""
    return rail_icon(name, color=color, px=px)


class AudioStrip(QFrame):
    volume_changed = Signal(int)
    muted_changed = Signal(bool)
    mic_volume_changed = Signal(int)
    mic_muted_changed = Signal(bool)
    input_device_picked = Signal(object)   # device id (impl-defined)
    output_device_picked = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AudioStrip")
        self.setFixedHeight(54)

        self._inputs: list[tuple[object, str]] = []
        self._outputs: list[tuple[object, str]] = []
        self._muted = False
        self._mic_muted = False

        # --- Mic (input) MUTE toggle + adjacent chevron device picker ---
        self.mic_btn = QToolButton(self)
        self.mic_btn.setObjectName("AudioBtn")
        self.mic_btn.setIcon(_icon("mic"))
        self.mic_btn.setIconSize(QSize(18, 18))
        self.mic_btn.setCheckable(True)
        self.mic_btn.setToolTip("Mute microphone")
        self.mic_btn.toggled.connect(self._on_mic_muted_toggled)

        # Tiny chevron next to mic to open input-device picker.
        self.mic_dev_btn = QToolButton(self)
        self.mic_dev_btn.setObjectName("AudioChevron")
        self.mic_dev_btn.setText("▾")
        self.mic_dev_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.mic_dev_btn.setToolTip("Input device")

        # --- Speaker (output) MUTE toggle + adjacent chevron picker ---
        self.spk_btn = QToolButton(self)
        self.spk_btn.setObjectName("AudioBtn")
        self.spk_btn.setIcon(_icon("speaker"))
        self.spk_btn.setIconSize(QSize(18, 18))
        self.spk_btn.setCheckable(True)
        self.spk_btn.setToolTip("Mute output")
        self.spk_btn.toggled.connect(self._on_muted_toggled)

        self.spk_dev_btn = QToolButton(self)
        self.spk_dev_btn.setObjectName("AudioChevron")
        self.spk_dev_btn.setText("▾")
        self.spk_dev_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.spk_dev_btn.setToolTip("Output device")

        # --- Mic volume button + popover ----------------------------
        # Just the number (e.g. "75") -- the mic icon to the left is
        # already the visual signifier.
        self.mic_vol_btn = QToolButton(self)
        self.mic_vol_btn.setObjectName("AudioBtn")
        self.mic_vol_btn.setText("75")
        self.mic_vol_btn.setMinimumWidth(32)
        self.mic_vol_btn.setToolTip("Microphone gain")
        self.mic_vol_btn.clicked.connect(self._toggle_mic_popover)

        self.mic_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_slider.setObjectName("VolumeSlider")
        self.mic_slider.setRange(0, 100)
        self.mic_slider.setValue(75)
        self.mic_slider.valueChanged.connect(self._on_mic_volume_changed)

        # --- Output volume button + popover -------------------------
        self.vol_btn = QToolButton(self)
        self.vol_btn.setObjectName("AudioBtn")
        self.vol_btn.setText("75")
        self.vol_btn.setMinimumWidth(32)
        self.vol_btn.setToolTip("Output volume")
        self.vol_btn.clicked.connect(self._toggle_volume_popover)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setObjectName("VolumeSlider")
        self.slider.setRange(0, 100)
        self.slider.setValue(75)
        self.slider.valueChanged.connect(self._on_volume_changed)

        # Last non-zero gain so muting → "0" can be undone back to it.
        self._mic_volume_pre_mute = 75
        self._volume_pre_mute = 75

        # Legacy attribute kept for any callers expecting an
        # `out_menu_btn`. The new chevron above (`spk_dev_btn`) replaces
        # its function.
        self.out_menu_btn = self.spk_dev_btn

        # --- TX / RX live audio level bars (driven by phone_shell) ----
        self.tx_bar = QProgressBar(self)
        self.tx_bar.setObjectName("AudioMeterTX")
        self.tx_bar.setRange(0, 100)
        self.tx_bar.setValue(0)
        self.tx_bar.setTextVisible(False)
        self.tx_bar.setFixedHeight(6)
        self.tx_bar.setFixedWidth(120)
        self.rx_bar = QProgressBar(self)
        self.rx_bar.setObjectName("AudioMeterRX")
        self.rx_bar.setRange(0, 100)
        self.rx_bar.setValue(0)
        self.rx_bar.setTextVisible(False)
        self.rx_bar.setFixedHeight(6)
        self.rx_bar.setFixedWidth(120)
        tx_label = QLabel("TX", self); tx_label.setObjectName("AudioMeterLabel")
        rx_label = QLabel("RX", self); rx_label.setObjectName("AudioMeterLabel")

        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.setSpacing(2)
        # Mic group: [icon] [chevron] [number]
        controls_row.addWidget(self.mic_btn)
        controls_row.addWidget(self.mic_dev_btn)
        controls_row.addWidget(self.mic_vol_btn)
        controls_row.addSpacing(12)
        # Speaker group: [icon] [chevron] [number]
        controls_row.addWidget(self.spk_btn)
        controls_row.addWidget(self.spk_dev_btn)
        controls_row.addWidget(self.vol_btn)
        controls_row.addStretch(1)

        meters_row = QHBoxLayout()
        meters_row.setContentsMargins(0, 0, 0, 0)
        meters_row.setSpacing(6)
        meters_row.addWidget(tx_label)
        meters_row.addWidget(self.tx_bar)
        meters_row.addSpacing(8)
        meters_row.addWidget(rx_label)
        meters_row.addWidget(self.rx_bar)
        meters_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(2)
        layout.addLayout(controls_row)
        layout.addLayout(meters_row)

        # Volume popovers (created lazily, hidden by default)
        self._vol_popover: QFrame | None = None
        self._mic_popover: QFrame | None = None

    # ------------------------------------------------------------------
    def set_tx_level(self, value: int) -> None:
        """0–100. Driven by mic level sample on the active call."""
        self.tx_bar.setValue(max(0, min(100, int(value))))

    def set_rx_level(self, value: int) -> None:
        """0–100. Driven by remote audio level sample on the active call."""
        self.rx_bar.setValue(max(0, min(100, int(value))))

    # ------------------------------------------------------------------
    def set_input_devices(self, devices: list[tuple[object, str]]) -> None:
        self._inputs = list(devices)
        menu = QMenu(self.mic_dev_btn)
        if not devices:
            empty = menu.addAction("No input devices")
            empty.setEnabled(False)
        else:
            for dev_id, label in devices:
                act = menu.addAction(label)
                act.triggered.connect(
                    lambda _checked=False, d=dev_id: self.input_device_picked.emit(d)
                )
        self.mic_dev_btn.setMenu(menu)

    def set_output_devices(self, devices: list[tuple[object, str]]) -> None:
        self._outputs = list(devices)
        menu = QMenu(self.spk_dev_btn)
        if not devices:
            empty = menu.addAction("No output devices")
            empty.setEnabled(False)
        else:
            for dev_id, label in devices:
                act = menu.addAction(label)
                act.triggered.connect(
                    lambda _checked=False, d=dev_id: self.output_device_picked.emit(d)
                )
        self.spk_dev_btn.setMenu(menu)

    # ------------------------------------------------------------------
    def set_volume(self, value: int) -> None:
        v = max(0, min(100, int(value)))
        self.slider.blockSignals(True)
        self.slider.setValue(v)
        self.slider.blockSignals(False)
        if v > 0:
            self._volume_pre_mute = v
        self.vol_btn.setText(str(v))

    def set_mic_volume(self, value: int) -> None:
        v = max(0, min(100, int(value)))
        self.mic_slider.blockSignals(True)
        self.mic_slider.setValue(v)
        self.mic_slider.blockSignals(False)
        if v > 0:
            self._mic_volume_pre_mute = v
        self.mic_vol_btn.setText(str(v))

    def _on_volume_changed(self, value: int) -> None:
        if value > 0:
            self._volume_pre_mute = value
        self.vol_btn.setText(str(value))
        self.volume_changed.emit(value)

    def _on_mic_volume_changed(self, value: int) -> None:
        if value > 0:
            self._mic_volume_pre_mute = value
        self.mic_vol_btn.setText(str(value))
        self.mic_volume_changed.emit(value)

    def _toggle_volume_popover(self) -> None:
        if self._vol_popover is None:
            pop = QFrame(self, Qt.WindowType.Popup)
            pop.setObjectName("VolumePopover")
            v = QVBoxLayout(pop)
            v.setContentsMargins(10, 8, 10, 8)
            v.setSpacing(4)
            label = QLabel("Output volume")
            label.setObjectName("VolumePopoverLabel")
            v.addWidget(label)
            self.slider.setMinimumWidth(160)
            v.addWidget(self.slider)
            self._vol_popover = pop
        if self._vol_popover.isVisible():
            self._vol_popover.hide()
            return
        anchor = self.vol_btn.mapToGlobal(QPoint(0, self.vol_btn.height()))
        self._vol_popover.move(anchor)
        self._vol_popover.show()

    def _toggle_mic_popover(self) -> None:
        if self._mic_popover is None:
            pop = QFrame(self, Qt.WindowType.Popup)
            pop.setObjectName("VolumePopover")
            v = QVBoxLayout(pop)
            v.setContentsMargins(10, 8, 10, 8)
            v.setSpacing(4)
            label = QLabel("Microphone gain")
            label.setObjectName("VolumePopoverLabel")
            v.addWidget(label)
            self.mic_slider.setMinimumWidth(160)
            v.addWidget(self.mic_slider)
            self._mic_popover = pop
        if self._mic_popover.isVisible():
            self._mic_popover.hide()
            return
        anchor = self.mic_vol_btn.mapToGlobal(QPoint(0, self.mic_vol_btn.height()))
        self._mic_popover.move(anchor)
        self._mic_popover.show()

    # ------------------------------------------------------------------
    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self.spk_btn.blockSignals(True)
        self.spk_btn.setChecked(self._muted)
        self.spk_btn.blockSignals(False)
        self.slider.setEnabled(not self._muted)
        # Mirror the gain label: muted shows "0"; unmuted restores the
        # last non-zero value the user had dialed.
        if self._muted:
            self.vol_btn.setText("0")
        else:
            self.vol_btn.setText(str(self._volume_pre_mute))

    def set_mic_muted(self, muted: bool) -> None:
        self._mic_muted = bool(muted)
        self.mic_btn.blockSignals(True)
        self.mic_btn.setChecked(self._mic_muted)
        self.mic_btn.blockSignals(False)
        self.mic_slider.setEnabled(not self._mic_muted)
        if self._mic_muted:
            self.mic_vol_btn.setText("0")
        else:
            self.mic_vol_btn.setText(str(self._mic_volume_pre_mute))

    def _on_muted_toggled(self, checked: bool) -> None:
        self._muted = checked
        self.slider.setEnabled(not checked)
        if checked:
            # Stash the current slider value (only if non-zero) so we
            # can restore it on unmute.
            cur = self.slider.value()
            if cur > 0:
                self._volume_pre_mute = cur
            self.vol_btn.setText("0")
        else:
            self.vol_btn.setText(str(self._volume_pre_mute))
        self.muted_changed.emit(checked)

    def _on_mic_muted_toggled(self, checked: bool) -> None:
        self._mic_muted = checked
        self.mic_slider.setEnabled(not checked)
        if checked:
            cur = self.mic_slider.value()
            if cur > 0:
                self._mic_volume_pre_mute = cur
            self.mic_vol_btn.setText("0")
        else:
            self.mic_vol_btn.setText(str(self._mic_volume_pre_mute))
        self.mic_muted_changed.emit(checked)
