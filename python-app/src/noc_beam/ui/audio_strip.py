"""Audio strip widget -- mic / speaker icons + volume slider with orange fill.

Mirrors the Bria pattern: a permanent strip near the top of the app
that always shows audio state. The slider drives speaker/output
volume; the mic icon is a dropdown for input device; speaker icon is a
mute toggle.

Public API:
    audio = AudioStrip()
    audio.set_input_devices(list[(id, label)])
    audio.set_output_devices(list[(id, label)])
    audio.set_volume(0..100)
    audio.set_muted(bool)
    Signals: volume_changed(int), muted_changed(bool),
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
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.ui.rail_icons import rail_icon


def _icon(name: str, color: str = "#57606A", px: int = 18) -> QIcon:
    """Reuse the rail-icon SVG renderer for the audio strip glyphs."""
    return rail_icon(name, color=color, px=px)


# Add minimal headphones / speaker / mic-off SVG paths to rail_icons via
# fall-through; if a name is missing rail_icon returns an empty QIcon.
# (Phase I.3 polish step adds real glyphs to rail_icons.py.)


class AudioStrip(QFrame):
    volume_changed = Signal(int)
    muted_changed = Signal(bool)
    input_device_picked = Signal(object)   # device id (impl-defined)
    output_device_picked = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AudioStrip")
        self.setFixedHeight(36)

        self._inputs: list[tuple[object, str]] = []
        self._outputs: list[tuple[object, str]] = []
        self._muted = False

        # --- Mic (input) button + dropdown -------------------------
        self.mic_btn = QToolButton(self)
        self.mic_btn.setObjectName("AudioBtn")
        self.mic_btn.setIcon(_icon("mic"))
        self.mic_btn.setIconSize(QSize(18, 18))
        self.mic_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.mic_btn.setToolTip("Input device")

        # --- Speaker (output) button = mute toggle -----------------
        self.spk_btn = QToolButton(self)
        self.spk_btn.setObjectName("AudioBtn")
        self.spk_btn.setIcon(_icon("speaker"))
        self.spk_btn.setIconSize(QSize(18, 18))
        self.spk_btn.setCheckable(True)
        self.spk_btn.setToolTip("Mute output")
        self.spk_btn.toggled.connect(self._on_muted_toggled)

        # --- Volume control: a button that opens a popover with the slider.
        # The slider used to live full-width in the strip and dominated the
        # top of the app like a media-player widget; tucking it into a popover
        # restores the audio strip to its proper status-bar role.
        self.vol_btn = QToolButton(self)
        self.vol_btn.setObjectName("AudioBtn")
        self.vol_btn.setText("75%")
        self.vol_btn.setToolTip("Output volume")
        self.vol_btn.clicked.connect(self._toggle_volume_popover)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setObjectName("VolumeSlider")
        self.slider.setRange(0, 100)
        self.slider.setValue(75)
        self.slider.valueChanged.connect(self._on_volume_changed)

        # --- Output dropdown (chevron on the right) ----------------
        self.out_menu_btn = QToolButton(self)
        self.out_menu_btn.setObjectName("AudioBtn")
        self.out_menu_btn.setText("Out")
        self.out_menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.out_menu_btn.setToolTip("Output device")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(8)
        layout.addWidget(self.mic_btn)
        layout.addWidget(self.spk_btn)
        layout.addWidget(self.vol_btn)
        layout.addStretch(1)
        layout.addWidget(self.out_menu_btn)

        # Volume popover (created lazily, hidden by default)
        self._vol_popover: QFrame | None = None

    # ------------------------------------------------------------------
    def set_input_devices(self, devices: list[tuple[object, str]]) -> None:
        self._inputs = list(devices)
        menu = QMenu(self.mic_btn)
        if not devices:
            empty = menu.addAction("No input devices")
            empty.setEnabled(False)
        else:
            for dev_id, label in devices:
                act = menu.addAction(label)
                act.triggered.connect(
                    lambda _checked=False, d=dev_id: self.input_device_picked.emit(d)
                )
        self.mic_btn.setMenu(menu)

    def set_output_devices(self, devices: list[tuple[object, str]]) -> None:
        self._outputs = list(devices)
        menu = QMenu(self.out_menu_btn)
        if not devices:
            empty = menu.addAction("No output devices")
            empty.setEnabled(False)
        else:
            for dev_id, label in devices:
                act = menu.addAction(label)
                act.triggered.connect(
                    lambda _checked=False, d=dev_id: self.output_device_picked.emit(d)
                )
        self.out_menu_btn.setMenu(menu)

    def set_volume(self, value: int) -> None:
        v = max(0, min(100, int(value)))
        self.slider.blockSignals(True)
        self.slider.setValue(v)
        self.slider.blockSignals(False)
        self.vol_btn.setText(f"{v}%")

    def _on_volume_changed(self, value: int) -> None:
        self.vol_btn.setText(f"{value}%")
        self.volume_changed.emit(value)

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
        # Anchor below the volume button.
        anchor = self.vol_btn.mapToGlobal(QPoint(0, self.vol_btn.height()))
        self._vol_popover.move(anchor)
        self._vol_popover.show()

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self.spk_btn.blockSignals(True)
        self.spk_btn.setChecked(self._muted)
        self.spk_btn.blockSignals(False)
        # Visual: when muted, dim the slider track via dynamic property
        self.slider.setEnabled(not self._muted)

    def _on_muted_toggled(self, checked: bool) -> None:
        self._muted = checked
        self.slider.setEnabled(not checked)
        self.muted_changed.emit(checked)
