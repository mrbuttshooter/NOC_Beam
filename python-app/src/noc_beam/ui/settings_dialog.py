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
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QToolButton,
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
    test_register_requested = Signal()

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
        # FooterActionBar is the contract objectName the dialog-redesign
        # test asserts on. SettingsFooter is the historical selector
        # that dark.qss styles. Set the former as objectName and the
        # latter as a dynamic property so both lookups still hit.
        footer.setObjectName("FooterActionBar")
        footer.setProperty("class", "SettingsFooter")
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
        from noc_beam import __app_name__, __version__
        from PySide6.QtWidgets import QSizePolicy, QSpacerItem
        from noc_beam.config.paths import data_dir

        w = QWidget()
        w.setObjectName("SettingsPane")
        layout = QVBoxLayout(w)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        title = QLabel("General")
        title.setObjectName("SettingsTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "App-wide preferences. Audio devices, codecs, theme, account "
            "credentials, and advanced SIP settings live in their own panes."
        )
        subtitle.setObjectName("SettingsSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # --- Appearance card -----------------------------------------
        appearance_card = QFrame()
        appearance_card.setObjectName("SettingsCard")
        a_l = QVBoxLayout(appearance_card)
        a_l.setContentsMargins(18, 16, 18, 16)
        a_l.setSpacing(8)
        a_label = QLabel("APPEARANCE")
        a_label.setObjectName("SettingsCardLabel")
        a_l.addWidget(a_label)
        # Promote the theme picker here so the user sees it on General.
        # The dedicated Appearance pane still has it (same widget).
        self._general_theme_combo = QComboBox()
        self._general_theme_combo.addItem("Light", "light")
        self._general_theme_combo.addItem("Dark", "dark")
        # Mirror current selection from the main theme_combo.
        try:
            current_theme = getattr(self._settings.appearance, "theme", "light")
            idx = self._general_theme_combo.findData(current_theme)
            if idx >= 0:
                self._general_theme_combo.setCurrentIndex(idx)

            # Sync the General combo's pick onto theme_combo via DATA,
            # not display text. Appearance pane's theme_combo items are
            # labeled "Light (Bria-style)" / "Dark (NOC dashboard)" so
            # setCurrentText("Dark") used to fail silently -- which is
            # why picking Dark in General never actually changed the
            # theme.
            def _sync_theme_to_appearance(text: str) -> None:
                if not hasattr(self, "theme_combo"):
                    return
                # Map our label ("Light" / "Dark") -> data key.
                data = self._general_theme_combo.currentData()
                if not data:
                    return
                idx = self.theme_combo.findData(data)
                if idx >= 0:
                    self.theme_combo.setCurrentIndex(idx)
            self._general_theme_combo.currentTextChanged.connect(_sync_theme_to_appearance)
        except Exception:
            pass
        theme_row = QHBoxLayout()
        theme_row.setContentsMargins(0, 0, 0, 0)
        theme_lbl = QLabel("Theme")
        theme_lbl.setObjectName("SettingsRowLabel")
        theme_lbl.setMinimumWidth(140)
        theme_row.addWidget(theme_lbl)
        theme_row.addWidget(self._general_theme_combo, 1)
        a_l.addLayout(theme_row)
        a_hint = QLabel("Applied immediately — no restart needed.")
        a_hint.setObjectName("SettingsRowHint")
        a_l.addWidget(a_hint)
        layout.addWidget(appearance_card)

        # --- Startup card --------------------------------------------
        startup_card = QFrame()
        startup_card.setObjectName("SettingsCard")
        s_l = QVBoxLayout(startup_card)
        s_l.setContentsMargins(18, 16, 18, 16)
        s_l.setSpacing(6)
        s_label = QLabel("STARTUP")
        s_label.setObjectName("SettingsCardLabel")
        s_l.addWidget(s_label)
        # Three optional checkboxes (functionality stubbed for now -- saved
        # but not yet read by the launcher).
        self._start_with_windows = QCheckBox("Start NOC_Beam when I sign in to Windows")
        self._start_minimized = QCheckBox("Start minimized to the system tray")
        self._restore_window_pos = QCheckBox("Restore previous window position")
        for box in (self._start_with_windows, self._start_minimized, self._restore_window_pos):
            box.setObjectName("SettingsCheckbox")
            s_l.addWidget(box)
        layout.addWidget(startup_card)

        # --- About card ----------------------------------------------
        about_card = QFrame()
        about_card.setObjectName("SettingsCard")
        ab_l = QVBoxLayout(about_card)
        ab_l.setContentsMargins(18, 16, 18, 16)
        ab_l.setSpacing(8)
        ab_label = QLabel("ABOUT")
        ab_label.setObjectName("SettingsCardLabel")
        ab_l.addWidget(ab_label)
        ver_row = QHBoxLayout()
        ver_lbl = QLabel(f"{__app_name__}")
        ver_lbl.setObjectName("SettingsRowLabel")
        ver_lbl.setMinimumWidth(140)
        ver_val = QLabel(f"v{__version__}")
        ver_val.setObjectName("SettingsRowValue")
        ver_row.addWidget(ver_lbl)
        ver_row.addWidget(ver_val, 1)
        ab_l.addLayout(ver_row)
        # Open log / data folder shortcuts -- one of the most-requested
        # NOC affordances per the audit.
        link_row = QHBoxLayout()
        link_row.setContentsMargins(0, 4, 0, 0)
        link_row.setSpacing(12)
        open_data_btn = QPushButton("Open user data folder")
        open_data_btn.setObjectName("SettingsLinkBtn")
        open_data_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def _open_data_folder() -> None:
            import os
            import subprocess
            p = str(data_dir())
            try:
                os.startfile(p)  # noqa: S606
            except AttributeError:
                subprocess.Popen(["xdg-open", p])
            except Exception:
                pass
        open_data_btn.clicked.connect(_open_data_folder)
        link_row.addWidget(open_data_btn)
        link_row.addStretch(1)
        ab_l.addLayout(link_row)
        layout.addWidget(about_card)

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
        from PySide6.QtWidgets import QScrollArea
        w = QWidget()
        w.setObjectName("SettingsPane")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        title = QLabel("Codecs")
        title.setObjectName("SettingsTitle")
        outer.addWidget(title)
        subtitle = QLabel(
            "Higher priority = preferred earlier in SIP offers/answers. "
            "0 disables. Use ▲ / ▼ to bump priority by 5."
        )
        subtitle.setObjectName("SettingsSubtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        # Single card wraps the codec list (modern, card-based UI).
        card = QFrame()
        card.setObjectName("SettingsCard")
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(0, 0, 0, 0)
        card_l.setSpacing(0)

        # Header strip
        header = QFrame()
        header.setObjectName("CodecListHeader")
        h_l = QHBoxLayout(header)
        h_l.setContentsMargins(18, 12, 18, 12)
        h_l.setSpacing(12)
        h_name = QLabel("CODEC")
        h_name.setObjectName("CodecListHeaderLabel")
        h_prio = QLabel("PRIORITY")
        h_prio.setObjectName("CodecListHeaderLabel")
        h_prio.setFixedWidth(110)
        h_prio.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h_actions = QLabel("")
        h_actions.setFixedWidth(70)
        h_l.addWidget(h_name, 1)
        h_l.addWidget(h_prio)
        h_l.addWidget(h_actions)
        card_l.addWidget(header)

        # codec_id → QSpinBox map for apply_to to read directly.
        self._codec_priority_spins: dict[str, QSpinBox] = {}

        codecs = list_codecs()
        for c in codecs:
            row = QFrame()
            row.setObjectName("CodecRow")
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(18, 10, 18, 10)
            row_l.setSpacing(12)

            # Name column
            name_col = QFrame()
            name_col_l = QVBoxLayout(name_col)
            name_col_l.setContentsMargins(0, 0, 0, 0)
            name_col_l.setSpacing(2)
            name_lbl = QLabel(c.codec_id.split("/", 1)[0])
            name_lbl.setObjectName("CodecRowName")
            sub_lbl = QLabel(c.display_name)
            sub_lbl.setObjectName("CodecRowSub")
            name_col_l.addWidget(name_lbl)
            name_col_l.addWidget(sub_lbl)

            # Priority spin
            spin = QSpinBox()
            spin.setRange(0, 255)
            stored = self._lookup_stored_priority(c.codec_id)
            spin.setValue(stored if stored is not None else c.priority)
            spin.setFixedWidth(110)
            spin.setObjectName("CodecRowPriority")
            spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._codec_priority_spins[c.codec_id] = spin

            # Reorder buttons
            up_btn = QToolButton()
            up_btn.setObjectName("CodecRowReorderBtn")
            up_btn.setText("▲")
            up_btn.setFixedSize(28, 28)
            up_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            up_btn.setToolTip("Boost priority by 5")
            up_btn.clicked.connect(
                lambda _checked=False, s=spin: s.setValue(min(255, s.value() + 5))
            )
            down_btn = QToolButton()
            down_btn.setObjectName("CodecRowReorderBtn")
            down_btn.setText("▼")
            down_btn.setFixedSize(28, 28)
            down_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            down_btn.setToolTip("Lower priority by 5")
            down_btn.clicked.connect(
                lambda _checked=False, s=spin: s.setValue(max(0, s.value() - 5))
            )
            reorder_wrap = QFrame()
            reorder_l = QHBoxLayout(reorder_wrap)
            reorder_l.setContentsMargins(0, 0, 0, 0)
            reorder_l.setSpacing(4)
            reorder_l.addWidget(up_btn)
            reorder_l.addWidget(down_btn)
            reorder_wrap.setFixedWidth(70)

            row_l.addWidget(name_col, 1)
            row_l.addWidget(spin)
            row_l.addWidget(reorder_wrap)
            card_l.addWidget(row)

        outer.addWidget(card)
        outer.addStretch(1)
        # Legacy compat shim: apply_to in older revisions iterated
        # codec_table.rowCount() / cellWidget. We now read from
        # _codec_priority_spins directly; keep a None-valued table
        # alias to avoid AttributeError from any external caller.
        self.codec_table = None
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
        # Two-way sync with the General pane's mirror combo. Previously
        # only General -> Appearance was wired, so picking Dark in
        # Appearance left General's combo showing stale "Light" until
        # next open. Round-trip the data key via findData so the
        # display-label difference between the two combos doesn't
        # break the match (Appearance has "Light (Bria-style)",
        # General has "Light").
        def _sync_back_to_general():
            gen = getattr(self, "_general_theme_combo", None)
            if gen is None:
                return
            data = self.theme_combo.currentData()
            gidx = gen.findData(data)
            if gidx >= 0 and gen.currentIndex() != gidx:
                gen.blockSignals(True)
                gen.setCurrentIndex(gidx)
                gen.blockSignals(False)
        self.theme_combo.currentIndexChanged.connect(
            lambda _i: _sync_back_to_general()
        )
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
        # Live-update the pill from sip_events for the current account.
        # Was stuck at "Unknown" forever -- a dead control. Now it
        # mirrors the registrar state in real time while the dialog
        # is open.
        from noc_beam.sip.events import sip_events as _sev
        def _update_pill(account_id: str, code: int, _reason: str,
                         _pill=status_pill, _aid=self._account.id):
            if account_id != _aid:
                return
            if code == 0:
                _pill.setText("● Unknown"); level = "muted"
            elif 200 <= code < 300:
                _pill.setText(f"● Registered ({code})"); level = "ok"
            elif code in (401, 403, 407):
                _pill.setText(f"● Auth failed ({code})"); level = "danger"
            else:
                _pill.setText(f"● Error ({code})"); level = "warn"
            _pill.setProperty("level", level)
            _pill.style().unpolish(_pill); _pill.style().polish(_pill)
        self._pill_slot = _update_pill
        _sev().registration_changed.connect(_update_pill)
        # Disconnect when the dialog goes away (it's modal but still
        # outlives a single tick; without disconnect we'd leak a
        # subscriber per Settings open).
        self.destroyed.connect(
            lambda *_: self._safe_disconnect_pill()
        )
        reg.addRow("Status",     status_pill)
        reg.addRow("Expires In", QLabel("—"))
        outer.addLayout(reg)

        test_btn = QPushButton("Test Register")
        test_btn.setObjectName("PrimaryAction")
        test_btn.setMinimumHeight(32)
        # Wired: emit test_register_requested so the host can route
        # through the same flow the standalone AccountDialog uses.
        # Previously the button existed but was a dead control with
        # no click handler.
        test_btn.clicked.connect(self.test_register_requested.emit)
        outer.addWidget(test_btn, 0, Qt.AlignmentFlag.AlignLeft)

        outer.addStretch(1)
        return w

    def _safe_disconnect_pill(self) -> None:
        """Drop the registration_changed subscriber installed in
        _build_account_pane. Hooked from destroyed so the singleton
        sip_events doesn't keep firing into the dead dialog."""
        from noc_beam.sip.events import sip_events as _sev
        slot = getattr(self, "_pill_slot", None)
        if slot is None:
            return
        try:
            _sev().registration_changed.disconnect(slot)
        except Exception:
            pass

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
        #
        # The previous "fallback to accept() if no receivers" was buggy:
        # QObject.receivers() with the *bound-signal* form is unreliable
        # in PySide6 (returns 0 even when slots are connected via the
        # Pythonic .connect call), so Apply silently closed the dialog
        # instead of applying -- defeating the whole "keep tweaking"
        # intent. Just emit; PhoneShell._on_settings always connects.
        self.apply_requested.emit()

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
        # Codecs -- reset each spinbox to the priority pjsua2 currently
        # advertises for that codec. Previously this was a comment that
        # left codecs untouched; users hitting "Reset settings" reasonably
        # expected the codec list to revert too, not stay at whatever
        # they last set. We re-read list_codecs() (which returns the
        # PJSIP-live priority, distinct from the GlobalSettings-stored
        # priority) so the dialog's view matches what's actually
        # running.
        try:
            from noc_beam.codecs.manager import list_codecs as _live_codecs
            spins = getattr(self, "_codec_priority_spins", {})
            for codec in _live_codecs():
                spin = spins.get(codec.codec_id)
                if spin is None:
                    continue
                try:
                    spin.setValue(int(codec.priority))
                except Exception:
                    pass
        except Exception:
            pass

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
        # Read from the codec_id -> QSpinBox dict built by the new
        # _build_codec_pane. Falls back to {} if PJSIP wasn't loaded
        # when Settings opened (empty dict = don't overwrite).
        spins = getattr(self, "_codec_priority_spins", {})
        for codec_id, spin in spins.items():
            try:
                prio = int(spin.value())
            except Exception:
                continue
            codec_map[codec_id] = prio
            new_priorities[codec_id] = prio
        # Merge instead of replace: PJSIP may load a SUBSET of installed
        # codecs (e.g. opus disabled in this build) and the spins dict
        # only contains the loaded ones. Replacing wholesale would wipe
        # priorities for codecs the user configured before but that
        # aren't currently visible -- a silent data loss every time a
        # PJSIP build dropped a codec for any reason.
        if spins:
            merged = dict(settings.codecs.priorities)
            merged.update(new_priorities)
            settings.codecs.priorities = merged
        return codec_map
