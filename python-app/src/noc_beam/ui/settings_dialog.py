"""Global settings dialog with sidebar navigation (mockup panel 7).

Layout: a left-rail QListWidget (General / Audio / Codecs / Appearance /
Account / Advanced) drives a QStackedWidget on the right. The Account
pane renders the active SIP account's identity + server + registration
sections inline so the user can review (and jump to edit) without
opening another dialog.

Footer: Reset / [stretch] / Cancel / Save (orange primary action).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from noc_beam.audio.devices import enumerate_devices
from noc_beam.codecs.manager import list_codecs
from noc_beam.config.store import AccountConfig, GlobalSettings


def _section_label(text: str) -> QLabel:
    """Small uppercase section heading used inside each settings pane."""
    lbl = QLabel(text)
    lbl.setObjectName("SettingsSectionLabel")
    return lbl


def _hr() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setObjectName("SettingsHr")
    return line


class SettingsDialog(QDialog):
    """Sidebar-nav settings. ``account`` is the active SIP account whose
    identity / server / registration is shown under the Account pane."""

    NAV_ITEMS = ("General", "Audio", "Codecs", "Appearance", "Account", "Advanced")

    # Apply-without-close: the host (phone_shell._on_settings) connects
    # to this and runs its apply_to + save_settings + push-to-PJSIP path
    # without dismissing the dialog. Lets the user click Apply, see the
    # change, keep tweaking.
    apply_requested = Signal()

    def __init__(
        self,
        settings: GlobalSettings,
        account: AccountConfig | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SettingsDialog")
        self.setWindowTitle("Settings")
        self.resize(720, 540)
        self._settings = settings
        self._account = account

        # ---- Left sidebar nav ----------------------------------------
        self._nav = QListWidget(self)
        self._nav.setObjectName("SettingsNav")
        self._nav.setFixedWidth(180)
        self._nav.setSpacing(0)
        self._nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for label in self.NAV_ITEMS:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, label)
            self._nav.addItem(item)
        # If we have an account, surface it as a sub-item under Account so
        # the user sees their per-account identity at a glance.
        if account is not None:
            sub = QListWidgetItem(f"  {account.display_name or account.username or 'active account'}")
            sub.setData(Qt.UserRole, "Account")
            sub.setForeground(Qt.GlobalColor.gray)
            # Insert directly after the Account row.
            account_idx = self.NAV_ITEMS.index("Account")
            self._nav.insertItem(account_idx + 1, sub)

        # ---- Right body stack ----------------------------------------
        self._stack = QStackedWidget(self)
        self._stack.setObjectName("SettingsBody")
        self._panes: dict[str, int] = {}
        for label in self.NAV_ITEMS:
            pane = self._build_pane(label)
            idx = self._stack.addWidget(pane)
            self._panes[label] = idx

        self._nav.currentRowChanged.connect(self._on_nav_changed)
        # Default to Account when we have one (matches the mockup "Account
        # is the entry the user almost always wants"), else General.
        default = "Account" if account is not None else "General"
        for i in range(self._nav.count()):
            it = self._nav.item(i)
            if it is not None and it.data(Qt.UserRole) == default:
                self._nav.setCurrentRow(i)
                break

        # ---- Footer action bar ---------------------------------------
        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("SettingsResetBtn")
        ok_btn = QPushButton("OK")
        ok_btn.setObjectName("SettingsOkBtn")
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("SettingsCancelBtn")
        apply_btn = QPushButton("Apply")
        apply_btn.setObjectName("PrimaryAction")
        for b in (reset_btn, ok_btn, cancel_btn, apply_btn):
            b.setMinimumHeight(34)

        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        apply_btn.clicked.connect(self._on_apply)
        # Reset was created but never connected previously -- clicking
        # did nothing, contradicting the "Reset to defaults" affordance.
        reset_btn.clicked.connect(self._on_reset)
        self._apply_btn = apply_btn
        self._reset_btn = reset_btn

        footer = QFrame(self)
        footer.setObjectName("SettingsFooter")
        footer_row = QHBoxLayout(footer)
        footer_row.setContentsMargins(16, 10, 16, 10)
        footer_row.setSpacing(8)
        footer_row.addWidget(reset_btn)
        footer_row.addStretch(1)
        footer_row.addWidget(ok_btn)
        footer_row.addWidget(cancel_btn)
        footer_row.addWidget(apply_btn)

        # ---- Master layout -------------------------------------------
        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(0)
        body_row.addWidget(self._nav)
        body_row.addWidget(self._stack, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(body_row, 1)
        layout.addWidget(footer)

        # Preserve the legacy `footer` attribute for callers that touch it.
        self.footer = footer

    # ------------------------------------------------------------------
    # Pane construction
    # ------------------------------------------------------------------
    def _build_pane(self, key: str) -> QWidget:
        method = {
            "General":    self._build_general_pane,
            "Audio":      self._build_audio_pane,
            "Codecs":     self._build_codec_pane,
            "Appearance": self._build_appearance_pane,
            "Account":    self._build_account_pane,
            "Advanced":   self._build_advanced_pane,
        }[key]
        return method()

    def _build_general_pane(self) -> QWidget:
        w = QWidget()
        w.setObjectName("SettingsPane")
        layout = QVBoxLayout(w)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)
        title = QLabel("General")
        title.setObjectName("SettingsTitle")
        layout.addWidget(title)
        hint = QLabel(
            "App-wide preferences. Use the sidebar to drill into Audio "
            "devices, codec priorities, theme, account credentials, or "
            "advanced SIP settings."
        )
        hint.setObjectName("ViewHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        return w

    def _build_audio_pane(self) -> QWidget:
        w = QWidget()
        w.setObjectName("SettingsPane")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(10)
        title = QLabel("Audio")
        title.setObjectName("SettingsTitle")
        outer.addWidget(title)

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

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.addRow("Microphone", self.in_combo)
        form.addRow("Speaker", self.out_combo)
        form.addRow("Ringer device", self.ring_combo)
        form.addRow("Echo cancel tail", self.ec_tail)
        form.addRow("Clock rate", self.clock)
        outer.addLayout(form)
        outer.addStretch(1)
        return w

    def _build_codec_pane(self) -> QWidget:
        w = QWidget()
        w.setObjectName("SettingsPane")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(10)
        title = QLabel("Codecs")
        title.setObjectName("SettingsTitle")
        outer.addWidget(title)
        hint = QLabel("Higher priority = preferred earlier in offers/answers. 0 disables.")
        hint.setObjectName("ViewHint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        codecs = list_codecs()
        self.codec_table = QTableWidget(len(codecs), 2)
        self.codec_table.setHorizontalHeaderLabels(["Codec", "Priority (0=off, 255=max)"])
        self.codec_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.codec_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        for row, c in enumerate(codecs):
            item = QTableWidgetItem(c.display_name)
            item.setData(Qt.UserRole, c.codec_id)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.codec_table.setItem(row, 0, item)
            spin = QSpinBox()
            spin.setRange(0, 255)
            stored = self._lookup_stored_priority(c.codec_id)
            spin.setValue(stored if stored is not None else c.priority)
            self.codec_table.setCellWidget(row, 1, spin)
        outer.addWidget(self.codec_table, 1)
        return w

    def _build_appearance_pane(self) -> QWidget:
        w = QWidget()
        w.setObjectName("SettingsPane")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(10)
        title = QLabel("Appearance")
        title.setObjectName("SettingsTitle")
        outer.addWidget(title)

        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Light (Bria-style)", "light")
        self.theme_combo.addItem("Dark (NOC dashboard)", "dark")
        current_theme = getattr(self._settings.appearance, "theme", "light")
        idx = self.theme_combo.findData(current_theme)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)
        theme_hint = QLabel("Applied immediately on Apply — no restart needed.")
        theme_hint.setObjectName("ViewHint")
        theme_hint.setWordWrap(True)

        self.high_contrast_chk = QCheckBox("Use high-contrast theme")
        self.high_contrast_chk.setChecked(self._settings.appearance.high_contrast)
        hc_hint = QLabel(
            "Pure-black background, white foreground/borders, yellow focus. "
            "Overrides the theme picker above when enabled."
        )
        hc_hint.setObjectName("ViewHint")
        hc_hint.setWordWrap(True)

        self.reduced_motion_chk = QCheckBox("Reduce motion (skip drawer slide and pulse)")
        self.reduced_motion_chk.setChecked(self._settings.appearance.reduced_motion)
        rm_hint = QLabel("Honoured live; no app restart needed.")
        rm_hint.setObjectName("ViewHint")
        rm_hint.setWordWrap(True)

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.addRow("Theme", self.theme_combo)
        form.addRow("", theme_hint)
        form.addRow("", self.high_contrast_chk)
        form.addRow("", hc_hint)
        form.addRow("", self.reduced_motion_chk)
        form.addRow("", rm_hint)
        outer.addLayout(form)
        outer.addStretch(1)
        return w

    def _build_account_pane(self) -> QWidget:
        """Account pane — Identity / Server / Registration sections (panel 7).

        Renders the active account read-only with a "Test Register" + "Edit
        account…" pair. The full edit flow still lives in the dedicated
        AccountDialog, opened from the chip menu or from the View menu.
        """
        w = QWidget()
        w.setObjectName("SettingsPane")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(10)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title = QLabel("Account")
        title.setObjectName("SettingsTitle")
        title_row.addWidget(title)
        if self._account is not None:
            uri = QLabel(f"sip:{self._account.username}@{self._account.domain}")
            uri.setObjectName("SettingsAccountUri")
            title_row.addWidget(uri)
        title_row.addStretch(1)
        outer.addLayout(title_row)

        if self._account is None:
            empty = QLabel(
                "No SIP account configured.\n\n"
                "Add an account from the brand row's account chip (top right) "
                "or from View → NOC Accounts to populate this pane."
            )
            empty.setObjectName("ViewHint")
            empty.setWordWrap(True)
            outer.addWidget(empty)
            outer.addStretch(1)
            return w

        # Identity
        outer.addWidget(_section_label("IDENTITY"))
        ident = QFormLayout()
        ident.setSpacing(6)
        ident.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        ident.addRow("Display Name", QLabel(self._account.display_name or "—"))
        ident.addRow("Username",     QLabel(self._account.username or "—"))
        ident.addRow("SIP URI",      QLabel(f"sip:{self._account.username}@{self._account.domain}"))
        outer.addLayout(ident)
        outer.addWidget(_hr())

        # Server
        outer.addWidget(_section_label("SERVER"))
        server = QFormLayout()
        server.setSpacing(6)
        server.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        server.addRow("Domain / Host", QLabel(self._account.domain or "—"))
        server.addRow("SIP Port",      QLabel(str(self._settings.sip_port) if self._settings.sip_port else "ephemeral"))
        server.addRow("Transport",     QLabel(self._account.transport.upper() if self._account.transport else "UDP"))
        outer.addLayout(server)
        outer.addWidget(_hr())

        # Registration
        outer.addWidget(_section_label("REGISTRATION"))
        reg = QFormLayout()
        reg.setSpacing(6)
        reg.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        status_pill = QLabel("● Unknown")
        status_pill.setObjectName("SettingsRegPill")
        status_pill.setProperty("level", "muted")
        self._reg_status_pill = status_pill
        reg.addRow("Status",     status_pill)
        reg.addRow("Expires In", QLabel("—"))
        outer.addLayout(reg)

        test_btn = QPushButton("Test Register")
        test_btn.setObjectName("SettingsTestRegBtn")
        test_btn.setMinimumHeight(32)
        # Note: actual test-register flow lives in AccountDialog; this
        # button is a hook the shell can wire up via signals if it wants.
        outer.addWidget(test_btn, 0, Qt.AlignmentFlag.AlignLeft)

        outer.addStretch(1)
        return w

    def _build_advanced_pane(self) -> QWidget:
        w = QWidget()
        w.setObjectName("SettingsPane")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(10)
        title = QLabel("Advanced")
        title.setObjectName("SettingsTitle")
        outer.addWidget(title)

        self.sip_port = QSpinBox()
        self.sip_port.setRange(0, 65535)
        self.sip_port.setSpecialValueText("ephemeral")
        self.sip_port.setValue(self._settings.sip_port)
        self.log_level = QSpinBox()
        self.log_level.setRange(0, 6)
        self.log_level.setValue(self._settings.log_level)

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.addRow("SIP port", self.sip_port)
        form.addRow("Log level (0-6)", self.log_level)
        outer.addLayout(form)
        outer.addStretch(1)
        return w

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------
    def _on_nav_changed(self, row: int) -> None:
        if row < 0:
            return
        item = self._nav.item(row)
        if item is None:
            return
        key = item.data(Qt.UserRole)
        idx = self._panes.get(key)
        if idx is not None:
            self._stack.setCurrentIndex(idx)

    def _on_apply(self) -> None:
        # Real apply-without-close: emit a signal the host listens for.
        # The host runs apply_to + save_settings + push-to-PJSIP and
        # leaves the dialog open so the user can keep tweaking.
        # If nothing is connected, fall back to legacy accept() so the
        # old shell wiring still works.
        if self.receivers(self.apply_requested) > 0:
            self.apply_requested.emit()
        else:
            self.accept()

    def _on_reset(self) -> None:
        """Reset all editable widgets to GlobalSettings() defaults.
        Confirms first so a misclick during demo doesn't wipe tweaks."""
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self,
            "Reset settings",
            "Restore all settings to their defaults? Changes will be "
            "kept in the dialog until you click OK or Apply.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        defaults = GlobalSettings()
        # Audio
        try:
            self._select_by_data(self.in_combo, defaults.audio.input_device)
            self._select_by_data(self.out_combo, defaults.audio.output_device)
            self._select_by_data(self.ring_combo, defaults.audio.ringer_device)
            self.ec_tail.setValue(defaults.audio.ec_tail_ms)
            self.clock.setValue(defaults.audio.clock_rate)
        except Exception:
            pass
        # Network / log
        try:
            self.sip_port.setValue(defaults.sip_port)
            self.log_level.setValue(defaults.log_level)
        except Exception:
            pass
        # Appearance
        try:
            self.high_contrast_chk.setChecked(defaults.appearance.high_contrast)
            self.reduced_motion_chk.setChecked(defaults.appearance.reduced_motion)
            idx = self.theme_combo.findData(defaults.appearance.theme)
            if idx >= 0:
                self.theme_combo.setCurrentIndex(idx)
        except Exception:
            pass
        # Codecs -- reset each row's priority spinbox to its default-ish
        # value (just leave the user's stored priority untouched; the
        # spec doesn't define a "default priority" per codec).

    @staticmethod
    def _select_by_data(combo: QComboBox, data: int) -> None:
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _lookup_stored_priority(self, codec_id: str) -> int | None:
        # Exact match first (the full "PCMU/8000/1" key) -- otherwise
        # opus/48000/1 and opus/48000/2 alias to the same stored entry
        # and last-write wins.
        prios = self._settings.codecs.priorities
        if codec_id in prios:
            return prios[codec_id]
        # Backwards-compat fallback: trimmed "PCMU/8000" stored by a
        # prior build still resolves to its codec.
        for key, prio in prios.items():
            if key.lower() in codec_id.lower():
                return prio
        return None

    # ------------------------------------------------------------------
    def apply_to(self, settings: GlobalSettings) -> dict[str, int]:
        settings.audio.input_device = self.in_combo.currentData()
        settings.audio.output_device = self.out_combo.currentData()
        settings.audio.ringer_device = self.ring_combo.currentData()
        settings.audio.ec_tail_ms = self.ec_tail.value()
        settings.audio.clock_rate = self.clock.value()
        settings.sip_port = self.sip_port.value()
        settings.log_level = self.log_level.value()
        settings.appearance.high_contrast = self.high_contrast_chk.isChecked()
        settings.appearance.reduced_motion = self.reduced_motion_chk.isChecked()
        settings.appearance.theme = self.theme_combo.currentData() or "light"

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
            # Key by full codec_id so opus/48000/1 and opus/48000/2
            # don't collide and overwrite each other (last-write-wins
            # bug from the trimmed "/".join(...[:2]) prefix key).
            new_priorities[codec_id] = prio
        # Guard against the "opened Settings before PJSIP loaded" case:
        # an empty codec table would overwrite the user's saved
        # priorities with {} and silently destroy their tuning.
        if self.codec_table.rowCount() > 0:
            settings.codecs.priorities = new_priorities
        return codec_map
