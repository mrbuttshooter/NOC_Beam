"""Global settings dialog: audio devices and codec priorities."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from noc_beam.audio.devices import enumerate_devices
from noc_beam.codecs.manager import list_codecs
from noc_beam.config.store import GlobalSettings


class SettingsDialog(QDialog):
    def __init__(self, settings: GlobalSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(560, 520)
        self._settings = settings

        tabs = QTabWidget()
        tabs.addTab(self._build_audio_tab(), "Audio")
        tabs.addTab(self._build_codec_tab(), "Codecs")
        tabs.addTab(self._build_appearance_tab(), "Appearance")
        tabs.addTab(self._build_advanced_tab(), "Advanced")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    def _build_audio_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        devices = enumerate_devices()

        self.in_combo = QComboBox()
        self.out_combo = QComboBox()
        self.ring_combo = QComboBox()
        for combo in (self.in_combo, self.out_combo, self.ring_combo):
            combo.addItem("System default", -1)
        for d in devices:
            label = f"{d.name} [{d.driver}]"
            if d.is_input:
                self.in_combo.addItem(label, d.index)
            if d.is_output:
                self.out_combo.addItem(label, d.index)
                self.ring_combo.addItem(label, d.index)

        self._select_by_data(self.in_combo, self._settings.audio.input_device)
        self._select_by_data(self.out_combo, self._settings.audio.output_device)
        self._select_by_data(self.ring_combo, self._settings.audio.ringer_device)

        self.ec_tail = QSpinBox()
        self.ec_tail.setRange(0, 500)
        self.ec_tail.setSuffix(" ms")
        self.ec_tail.setValue(self._settings.audio.ec_tail_ms)

        self.clock = QSpinBox()
        self.clock.setRange(8000, 48000)
        self.clock.setSingleStep(8000)
        self.clock.setSuffix(" Hz")
        self.clock.setValue(self._settings.audio.clock_rate)

        form.addRow("Microphone", self.in_combo)
        form.addRow("Speaker", self.out_combo)
        form.addRow("Ringer device", self.ring_combo)
        form.addRow("Echo cancel tail", self.ec_tail)
        form.addRow("Clock rate", self.clock)
        return w

    def _build_codec_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        codecs = list_codecs()

        self.codec_table = QTableWidget(len(codecs), 2)
        self.codec_table.setHorizontalHeaderLabels(["Codec", "Priority (0=off, 255=max)"])
        self.codec_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self.codec_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )

        for row, c in enumerate(codecs):
            item = QTableWidgetItem(c.display_name)
            item.setData(Qt.UserRole, c.codec_id)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.codec_table.setItem(row, 0, item)

            spin = QSpinBox()
            spin.setRange(0, 255)
            # Use stored priority if we have one matching, else live codec value
            stored = self._lookup_stored_priority(c.codec_id)
            spin.setValue(stored if stored is not None else c.priority)
            self.codec_table.setCellWidget(row, 1, spin)

        layout.addWidget(self.codec_table)
        return w

    def _build_appearance_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.high_contrast_chk = QCheckBox("Use high-contrast theme")
        self.high_contrast_chk.setChecked(self._settings.appearance.high_contrast)
        hc_hint = QLabel(
            "Pure-black background, white foreground/borders, yellow focus. "
            "Use when running on the on-call NOC desk or under bright glare."
        )
        hc_hint.setStyleSheet("color: #7C8696;")
        hc_hint.setWordWrap(True)

        self.reduced_motion_chk = QCheckBox("Reduce motion (skip drawer slide and pulse)")
        self.reduced_motion_chk.setChecked(self._settings.appearance.reduced_motion)
        rm_hint = QLabel(
            "Snaps the trace drawer open/closed and stops the LIVE pulse. "
            "Honoured live; no app restart needed."
        )
        rm_hint.setStyleSheet("color: #7C8696;")
        rm_hint.setWordWrap(True)

        form.addRow(self.high_contrast_chk)
        form.addRow(hc_hint)
        form.addRow(self.reduced_motion_chk)
        form.addRow(rm_hint)
        return w

    def _build_advanced_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.sip_port = QSpinBox()
        self.sip_port.setRange(0, 65535)
        self.sip_port.setSpecialValueText("ephemeral")
        self.sip_port.setValue(self._settings.sip_port)
        self.log_level = QSpinBox()
        self.log_level.setRange(0, 6)
        self.log_level.setValue(self._settings.log_level)
        form.addRow("SIP port", self.sip_port)
        form.addRow("Log level (0-6)", self.log_level)
        return w

    # ------------------------------------------------------------------
    def _lookup_stored_priority(self, codec_id: str) -> int | None:
        for key, prio in self._settings.codecs.priorities.items():
            if key.lower() in codec_id.lower():
                return prio
        return None

    @staticmethod
    def _select_by_data(combo: QComboBox, data: int) -> None:
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    # ------------------------------------------------------------------
    def apply_to(self, settings: GlobalSettings) -> dict[str, int]:
        """Mutates `settings` in place and returns codec-id -> priority map."""
        settings.audio.input_device = self.in_combo.currentData()
        settings.audio.output_device = self.out_combo.currentData()
        settings.audio.ringer_device = self.ring_combo.currentData()
        settings.audio.ec_tail_ms = self.ec_tail.value()
        settings.audio.clock_rate = self.clock.value()
        settings.sip_port = self.sip_port.value()
        settings.log_level = self.log_level.value()
        settings.appearance.high_contrast = self.high_contrast_chk.isChecked()
        settings.appearance.reduced_motion = self.reduced_motion_chk.isChecked()

        new_priorities: dict[str, int] = {}
        codec_map: dict[str, int] = {}
        for row in range(self.codec_table.rowCount()):
            item = self.codec_table.item(row, 0)
            spin = self.codec_table.cellWidget(row, 1)
            if item is None or spin is None:
                continue
            codec_id = item.data(Qt.UserRole)
            prio = int(spin.value())
            codec_map[codec_id] = prio
            # Store under "PROTO/RATE" key
            key = "/".join(codec_id.split("/")[:2])
            new_priorities[key] = prio
        settings.codecs.priorities = new_priorities
        return codec_map
