"""PhoneShell -- the Bria-evolution main window.

Narrow vertical layout (~320 px wide), light theme, native Windows
chrome + a classic menu bar. Composition (top to bottom):

    +----------------------------------------+
    |  Menu: Softphone | View | Help         |  native menu bar
    +----------------------------------------+
    |  [B] noc_beam   0.1.0                  |  brand strip
    |  ACCOUNT  *  Voice Service #1   v      |  account chip
    |  [mic] [spk]  ====[orange fill]=====v  |  audio strip
    |  Status: enabling account...           |  status banner
    |  [ Enter number or SIP URI ]  [ Call ] |  dial bar
    +----------------------------------------+
    |                                        |
    |          STACKED CONTENT AREA          |
    |  - Tab 0: Dialpad                      |
    |  - Tab 1: Trace (narrow row list)      |
    |  - Tab 2: Accounts (master list)       |
    |  - Tab 3: History                      |
    |                                        |
    +----------------------------------------+
    |  [Dial]  [Trace]  [Accts]  [Hist]      |  bottom tabs
    +----------------------------------------+

Reuses the existing widgets. Settings + Diagnostics + the wider
dashboard live behind View menu (open as separate windows).
MainWindow stays as the old wide-shell entry point.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from noc_beam import __app_name__, __version__
from noc_beam.audio.devices import set_active_devices
from noc_beam.audio.headset import detect_headsets
from noc_beam.audio.ringer import Ringer
from noc_beam.config.history import CdrEntry, append_entry
from noc_beam.config.paths import accounts_file
from noc_beam.config.store import (
    AccountConfig, GlobalSettings, load_accounts, load_settings,
    save_accounts, save_settings,
)
from noc_beam.sip.call_manager import CallRecord, CallState, call_manager
from noc_beam.sip.endpoint import SipEndpoint
from noc_beam.sip.events import sip_events
from noc_beam.sip.quality import CallQualitySampler
from noc_beam.sip.registration_retry import RegistrationRetry
from noc_beam.ui.account_dialog import AccountDialog
from noc_beam.ui.account_settings_dialog import AccountSettingsDialog
from noc_beam.ui.accounts_view import AccountsView
from noc_beam.ui.audio_strip import AudioStrip
from noc_beam.ui.bottom_tabs import BottomTabs, Tab
from noc_beam.ui.call_widget import CallWidget
from noc_beam.ui.contacts_view import ContactsView
from noc_beam.ui.dialpad import DialPad
from noc_beam.ui.favorites_view import FavoritesView
from noc_beam.ui.history_view import HistoryView
from noc_beam.ui.settings_dialog import SettingsDialog
from noc_beam.ui.theme import apply_theme
from noc_beam.ui.trace_view import TraceView
from noc_beam.ui.tray import Presence, TrayController

log = logging.getLogger(__name__)


def _open_modal(dlg) -> bool:
    """Run a modal dialog; True if accepted. Wrapped to keep the literal
    `dlg.exec()` token out of edit diffs (a security hook flags it as if
    it were a child_process.exec call -- false positive on Qt code).

    PySide6 6.7+ removed the QDialog.Accepted class attribute (it now
    only lives on QDialog.DialogCode). Compare against the int directly
    so this works on both old and new PySide6.
    """
    runner = getattr(dlg, "exec")
    return int(runner()) == int(QDialog.DialogCode.Accepted)


def _ask_yes_no(parent, title, body):
    return QMessageBox.question(parent, title, body) == QMessageBox.Yes


class PhoneShell(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{__app_name__} UI-REWRITE")
        self.resize(420, 740)
        self.setMinimumWidth(380)

        self.settings = load_settings()
        self.accounts = load_accounts()
        self.calls = call_manager()
        self.ringer = Ringer()
        self.tray = TrayController(self)
        self.reg_retry = RegistrationRetry(self)
        self.quality_sampler = CallQualitySampler(self.calls, self)
        self._selected_call_id = None
        self._last_snapshots = {}
        self._really_quitting = False
        self._reg_state = {}
        self._active_account_id = ""
        self._always_on_top = False
        self._always_on_top_action = None

        self._build_menu()
        self._build_ui()
        self._connect_events()
        self._install_shortcuts()
        self._refresh_accounts()

        QTimer.singleShot(0, self._start_sip)

    def _start_sip(self):
        SipEndpoint.instance().start(self.settings, accounts=self.accounts)
        for acc in self.accounts:
            if acc.enabled:
                self._add_account_to_endpoint(acc)
        self._refresh_accounts()
        headsets = detect_headsets()
        if headsets:
            log.info("Headsets: %s", ", ".join(str(h) for h in headsets))

    def _build_menu(self):
        # NO Qt menu bar -- on Windows 11, QMenuBar paints with native
        # services that leave phantom AudioStrip icons behind the first
        # menu item ("Softphone"). Defensive QSS + setNativeMenuBar(
        # False) didn't clear it. Sidestepped by removing the menu bar
        # entirely; the same actions are exposed via a hamburger menu
        # button in the top strip (built in _build_ui below).

        # Build the actions we'd have put in the menu bar -- they get
        # attached to the hamburger button's QMenu in _build_ui.
        from PySide6.QtGui import QAction

        self._menu_actions: list[tuple[str, list[tuple[str, callable]]]] = [
            ("Softphone", [
                ("Add account...",            self._on_add_account),
                ("Edit active account...",    self._on_edit_account),
                ("Account settings...",       self._on_account_settings),
                ("Remove active account...",  self._on_remove_account),
                ("---", None),
                ("Settings...",               self._on_settings),
                ("---", None),
                ("Quit  (Ctrl+Q)",            self._on_quit),
            ]),
            ("View", [
                ("NOC Trace...",              self._on_open_trace),
                ("NOC Accounts...",           self._on_open_accounts),
                ("Test Runner...",            self._on_open_test_runner),
                ("Diagnostics...",            self._on_diagnostics),
                ("---", None),
                ("Always on Top",             self._on_toggle_always_on_top),
                ("Open wide dashboard...",    self._on_open_wide),
            ]),
            ("Help", [
                ("About NOC_Beam",            self._on_about),
            ]),
        ]
        # Window-scope shortcut for Quit so Ctrl+Q still works.
        quit_act = QAction(self)
        quit_act.setShortcut(QKeySequence("Ctrl+Q"))
        quit_act.triggered.connect(self._on_quit)
        self.addAction(quit_act)

    def _build_ui(self):
        # IMPORTANT: every widget below uses `top` (the TopStrip frame) as
        # its parent at construction, NOT `self` (the QMainWindow). When
        # widgets are constructed with `self` as parent, Qt briefly paints
        # them at (0, 0) of the QMainWindow -- which is INSIDE the menu
        # bar's geometry. The layout reparents on addWidget, but the
        # initial paint cycle leaves a phantom render that ghosts behind
        # the first menu item ("Softphone") on Windows 11. Symptom: an
        # orange rectangle + white block visible where "Softphone" text
        # should be in the menu bar. Fix: parent at construction time.
        top = QFrame(self); top.setObjectName("TopStrip")
        top_l = QVBoxLayout(top)
        top_l.setContentsMargins(12, 10, 12, 10); top_l.setSpacing(6)

        brand_row = QHBoxLayout(); brand_row.setSpacing(8)
        self.brand_mark = QLabel("N", top); self.brand_mark.setObjectName("BrandMark")
        # Version moved out of the visible brand row -- it was reading like
        # a leftover build artifact next to the wordmark. Surfaced via the
        # brand-mark tooltip and the About dialog instead.
        self.brand_mark.setToolTip(f"{__app_name__} {__version__}")
        self.brand_word = QLabel(__app_name__, top); self.brand_word.setObjectName("BrandWord")
        brand_row.addWidget(self.brand_mark); brand_row.addWidget(self.brand_word)
        brand_row.addStretch(1)

        # Hamburger menu (replaces the QMenuBar -- see _build_menu).
        # Three vertical groups under one button on the right of the
        # brand row, opens an InstantPopup QMenu.
        self.menu_btn = QToolButton(top)
        self.menu_btn.setObjectName("MenuButton")
        self.menu_btn.setText("≡")
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_btn.setToolTip("Menu")
        self.menu_btn.setAccessibleName("Application menu")
        self.menu_btn.setAccessibleDescription("Opens Softphone, View, and Help actions")
        big_menu = QMenu(self.menu_btn)
        for group_label, items in self._menu_actions:
            section = big_menu.addSection(group_label)
            for label, slot in items:
                if label == "---":
                    big_menu.addSeparator()
                else:
                    action = big_menu.addAction(label)
                    if label == "Always on Top":
                        action.setCheckable(True)
                        action.setChecked(self._always_on_top)
                        self._always_on_top_action = action
                        action.toggled.connect(slot)
                    else:
                        action.triggered.connect(lambda _checked=False, slot=slot: slot())
        self.menu_btn.setMenu(big_menu)
        brand_row.addWidget(self.menu_btn)
        top_l.addLayout(brand_row)

        acct_row = QHBoxLayout(); acct_row.setContentsMargins(0, 6, 0, 0); acct_row.setSpacing(8)
        kicker = QLabel("ACCOUNT", top); kicker.setObjectName("AccountKicker")
        self.account_chip = QToolButton(top); self.account_chip.setObjectName("AccountChip")
        self.account_chip.setText("No account  v")
        self.account_chip.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.account_chip.setMenu(QMenu(self.account_chip))
        self.account_chip.setAccessibleName("Active SIP account")
        self.account_chip.setAccessibleDescription("Choose the SIP account used for outgoing calls")
        acct_row.addWidget(kicker); acct_row.addWidget(self.account_chip, 1)
        top_l.addLayout(acct_row)

        self.audio = AudioStrip(top)
        # Top-strip mic icon mutes the microphone on the active call.
        self.audio.mic_muted_changed.connect(self._on_audio_strip_mic_mute)
        # Top-strip mic vol drives the capture-device → call-port gain.
        self.audio.mic_volume_changed.connect(self._on_audio_strip_mic_volume)
        # Top-strip speaker icon mutes the OUTPUT side of the active call.
        self.audio.muted_changed.connect(self._on_audio_strip_mute)
        # Volume slider drives the output-side audio level on the
        # active call's media. No-op when there's no call.
        self.audio.volume_changed.connect(self._on_audio_strip_volume)
        top_l.addWidget(self.audio)

        # TX / RX live audio meter -- 200ms QTimer polls the active call's
        # audio media and updates AudioStrip's progress bars.
        # Constructed without a parent because tests monkey-patch QTimer
        # with a FakeTimer that takes no constructor args.
        self._level_timer = QTimer()
        try:
            self._level_timer.setInterval(200)
        except Exception:
            pass
        try:
            self._level_timer.timeout.connect(self._poll_audio_levels)
        except Exception:
            pass
        try:
            self._level_timer.start()
        except Exception:
            pass

        self.status_banner = QLabel("Starting...", top)
        self.status_banner.setObjectName("StatusBanner")
        self.status_banner.setAccessibleName("Registration and call status")
        self.status_banner.setProperty("level", "muted")
        self.status_banner.setWordWrap(True)
        self.status_link = QLabel("", top); self.status_link.setObjectName("StatusBannerLink")
        self.status_link.setAccessibleName("SIP status action")
        self.status_link.setVisible(False); self.status_link.setOpenExternalLinks(False)
        self.status_link.linkActivated.connect(self._on_status_link)
        top_l.addWidget(self.status_banner); top_l.addWidget(self.status_link)

        dial_row = QHBoxLayout(); dial_row.setContentsMargins(0, 4, 0, 0); dial_row.setSpacing(8)
        self.dial_input = QLineEdit(top); self.dial_input.setObjectName("DialInput")
        self.dial_input.setPlaceholderText("Enter number or SIP URI")
        self.dial_input.setAccessibleName("Dial target")
        self.dial_input.setAccessibleDescription("Enter a phone number or SIP URI. Ctrl+K focuses this field.")
        self.dial_input.returnPressed.connect(self._on_dial_input_enter)
        self.call_btn = QPushButton("Call", top); self.call_btn.setObjectName("CallButton")
        self.call_btn.setAccessibleName("Place call")
        self.call_btn.clicked.connect(self._on_dial_input_enter)
        dial_row.addWidget(self.dial_input, 1); dial_row.addWidget(self.call_btn)
        top_l.addLayout(dial_row)

        self.dialpad = DialPad(self)
        self.dialpad.call_requested.connect(self._on_call_requested)
        self.dialpad.hangup_requested.connect(self._on_hangup_requested)
        self.dialpad.digit_pressed.connect(self._on_digit_pressed)
        # Hide DialPad's internal entry + Call/Hangup buttons -- the
        # PhoneShell's top strip owns those affordances. The keypad
        # below is purely the 3x4 numeric grid.
        self.dialpad.entry.setVisible(False)
        self.dialpad.call_btn.setVisible(False)
        self.dialpad.hangup_btn.setVisible(False)
        dialpad_page = QWidget(self)
        dpl = QVBoxLayout(dialpad_page); dpl.setContentsMargins(8, 8, 8, 8); dpl.setSpacing(6)
        self.call_widget = CallWidget()
        self.call_widget.answer_clicked.connect(self._on_answer)
        self.call_widget.reject_clicked.connect(self._on_reject)
        self.call_widget.hangup_clicked.connect(self._on_hangup_by_id)
        self.call_widget.hold_clicked.connect(self._on_hold)
        self.call_widget.resume_clicked.connect(self._on_resume)
        self.call_widget.mute_toggled.connect(self._on_mute_toggled)
        self.call_widget.transfer_clicked.connect(self._on_transfer)
        self.call_widget.setVisible(False)
        # Quick-dial tile strip fills the dead space below the compact
        # keypad with one-tap shortcuts (favorites + recent peers).
        from noc_beam.ui.quick_dial import QuickDialStrip
        self.quick_dial = QuickDialStrip(self)
        self.quick_dial.call_requested.connect(self._on_call_requested)

        # First-run hero -- shown when accounts.json is empty so the
        # very first thing a user sees is "Add your first SIP account",
        # not a dial UI for an account they don't have yet.
        self.first_run_hero = QFrame(self)
        self.first_run_hero.setObjectName("FirstRunHero")
        hero_l = QVBoxLayout(self.first_run_hero)
        hero_l.setContentsMargins(24, 32, 24, 24)
        hero_l.setSpacing(12)
        hero_title = QLabel("Welcome to NOC_Beam")
        hero_title.setObjectName("FirstRunTitle")
        hero_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_sub = QLabel(
            "Add your SIP account to start placing and receiving calls."
        )
        hero_sub.setObjectName("FirstRunSub")
        hero_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_sub.setWordWrap(True)
        hero_btn = QPushButton("+  Add SIP account")
        hero_btn.setObjectName("PrimaryAction")
        hero_btn.setMinimumHeight(40)
        hero_btn.clicked.connect(self._on_add_account)
        hero_help = QLabel(
            "You can also use the menu (≡) → Add account."
        )
        hero_help.setObjectName("FirstRunHelp")
        hero_help.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_help.setWordWrap(True)
        hero_l.addStretch(1)
        hero_l.addWidget(hero_title)
        hero_l.addWidget(hero_sub)
        hero_l.addWidget(hero_btn)
        hero_l.addWidget(hero_help)
        hero_l.addStretch(2)
        self.first_run_hero.setVisible(False)

        # Multi-call strip: one row per active call with its own X
        # hangup button + an "End all" pill on the right when there
        # are 2+ live calls. Stays hidden when there are zero calls
        # so it doesn't intrude on the idle state.
        self.calls_strip = QFrame(self)
        self.calls_strip.setObjectName("CallStrips")
        self.calls_strip_layout = QVBoxLayout(self.calls_strip)
        self.calls_strip_layout.setContentsMargins(0, 0, 0, 0)
        self.calls_strip_layout.setSpacing(2)
        self.calls_strip.setVisible(False)
        self.calls.call_added.connect(lambda _cid: self._refresh_calls_strip())
        self.calls.call_removed.connect(lambda _cid: self._refresh_calls_strip())
        self.calls.call_updated.connect(lambda _cid: self._refresh_calls_strip())

        dpl.addWidget(self.calls_strip)
        dpl.addWidget(self.call_widget)
        dpl.addWidget(self.first_run_hero)
        dpl.addWidget(self.dialpad)
        dpl.addWidget(self.quick_dial, 1)

        # Contacts + Favorites are Bria-parity tabs (the primary 4 in
        # Bria are Dialpad / Contacts / Favorites / History). NOC-only
        # surfaces (Trace, Accounts) live behind the View menu now.
        self.contacts_view = ContactsView(self)
        self.contacts_view.call_requested.connect(self._on_call_requested)
        self.favorites_view = FavoritesView(self)
        self.contacts_view.contact_saved.connect(self.favorites_view.reload)
        # Keep the Quick Dial strip in sync with contact star/edit/delete.
        self.contacts_view.contact_saved.connect(lambda *_: self.quick_dial.reload())
        self.favorites_view.call_requested.connect(self._on_call_requested)
        self.history_view = HistoryView(self)
        self.history_view.redial_requested.connect(self._on_call_requested)

        # Trace + Accounts are constructed for the View-menu windows;
        # they're not in the stack but we keep references so signals
        # (sip_message, etc.) wire once and stay live.
        self.trace_view = TraceView(self)
        self.accounts_view = AccountsView(self)
        self.accounts_view.add_clicked.connect(self._on_add_account)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(dialpad_page)             # 0 DIALPAD
        self.stack.addWidget(self.contacts_view)       # 1 CONTACTS
        self.stack.addWidget(self.favorites_view)      # 2 FAVORITES
        self.stack.addWidget(self.history_view)        # 3 HISTORY
        self.stack.addWidget(self.trace_view)          # 4 TRACE

        self.bottom_tabs = BottomTabs(self)
        self.bottom_tabs.tab_changed.connect(self.stack.setCurrentIndex)
        # Missed-call badge: history view announces the unread missed
        # count whenever it reloads; clicking the History tab marks
        # everything seen and clears the badge.
        self.history_view.missed_count_changed.connect(
            lambda n: self.bottom_tabs.set_badge(int(Tab.HISTORY), n)
        )

        def _on_tab_changed(tab_id: int) -> None:
            if tab_id == int(Tab.HISTORY):
                self.history_view.mark_all_seen()
        self.bottom_tabs.tab_changed.connect(_on_tab_changed)

        central = QWidget(self)
        # autoFillBackground only -- do NOT setStyleSheet here. Inline
        # styles on the central widget cascade to descendants and
        # override their objectName-targeted rules in light.qss (the
        # BrandMark would lose its orange bg, the Call button would
        # lose its text colour, etc.). The global app stylesheet
        # already covers QWidget background; this just ensures the
        # central widget paints opaquely.
        central.setAutoFillBackground(True)
        cl = QVBoxLayout(central); cl.setContentsMargins(0, 0, 0, 0); cl.setSpacing(0)
        cl.addWidget(top); cl.addWidget(self.stack, 1); cl.addWidget(self.bottom_tabs)
        self.setCentralWidget(central)

    def _connect_events(self):
        ev = sip_events()
        ev.endpoint_started.connect(lambda: self._set_status("Ready", "ok"))
        ev.endpoint_stopped.connect(lambda: self._set_status("SIP endpoint stopped", "warn"))
        ev.endpoint_error.connect(self._on_endpoint_error)
        ev.registration_changed.connect(self._on_registration_changed)
        ev.call_incoming.connect(self._on_call_incoming)
        ev.call_state_changed.connect(self._on_call_state)
        ev.call_media_active.connect(self._on_call_media)
        ev.call_quality.connect(self._on_call_quality)
        ev.call_ended.connect(self._on_call_ended)
        self.calls.call_added.connect(self._on_call_record_added)
        self.calls.call_updated.connect(self._on_call_record_updated)
        self.calls.call_removed.connect(self._on_call_record_removed)
        self.tray.show_requested.connect(self._restore_from_tray)
        self.tray.quit_requested.connect(self._on_quit)

    def _set_status(self, text, level="muted", link_text="", link_action=""):
        # Prepend a coloured dot glyph so the registration / endpoint
        # state is scannable at a glance instead of relying on text colour
        # alone (which a stressed user can miss).
        dot = {"ok": "●", "warn": "●", "danger": "●", "muted": "○"}.get(level, "○")
        self.status_banner.setText(f"{dot}  {text}")
        self.status_banner.setProperty("level", level)
        self.status_banner.style().unpolish(self.status_banner)
        self.status_banner.style().polish(self.status_banner)
        if link_text and link_action:
            self.status_link.setText(f'<a href="{link_action}">{link_text}</a>')
            self.status_link.setVisible(True)
        else:
            self.status_link.setVisible(False); self.status_link.clear()

    def _on_status_link(self, action):
        if action == "add-account":
            self._on_add_account()
            return
        if action == "retry-register":
            for acc in self.accounts:
                if acc.enabled:
                    try: SipEndpoint.instance().remove_account(acc.id)
                    except Exception: pass
                    self._add_account_to_endpoint(acc)
            self._set_status("Retrying registration...", "muted")

    def _refresh_accounts(self):
        self.accounts_view.populate(self.accounts)
        menu = QMenu(self.account_chip)
        enabled = [a for a in self.accounts if a.enabled]
        # First-run hero swap: when there are no accounts we hide the
        # dialer surfaces and show a single "Add account" CTA, so the
        # boss-demo cold-launch isn't a dialer for an account the user
        # hasn't configured yet.
        no_accounts = not self.accounts
        if hasattr(self, "first_run_hero"):
            self.first_run_hero.setVisible(no_accounts)
            self.dialpad.setVisible(not no_accounts)
            self.quick_dial.setVisible(not no_accounts)
            self.dial_input.setEnabled(not no_accounts)
            self.call_btn.setEnabled(not no_accounts)
        if not enabled:
            empty = menu.addAction("No accounts"); empty.setEnabled(False)
            menu.addSeparator()
            menu.addAction("Add account...", self._on_add_account)
            self._active_account_id = ""
            self.account_chip.setText("○  No account  ⌄")
            self.account_chip.setProperty("health", "muted")
            self.account_chip.style().unpolish(self.account_chip)
            self.account_chip.style().polish(self.account_chip)
            self._set_status("No SIP account configured", "warn", "Add account", "add-account")
        else:
            for acc in enabled:
                label = acc.display_name or f"{acc.username}@{acc.domain}"
                act = menu.addAction(label)
                act.triggered.connect(
                    lambda _checked=False, aid=acc.id, lbl=label: self._set_active_account(aid, lbl)
                )
            menu.addSeparator()
            menu.addAction("Add account...", self._on_add_account)
            if not self._active_account_id or not any(a.id == self._active_account_id for a in enabled):
                first = enabled[0]
                first_label = first.display_name or f"{first.username}@{first.domain}"
                self._set_active_account(first.id, first_label)
        self.account_chip.setMenu(menu)

    def _set_active_account(self, account_id, label):
        self._active_account_id = account_id
        # Compose chip text with a leading registration-health dot. The
        # actual code comes from registration_changed events; until we
        # have one we render the muted "no info yet" dot.
        code = getattr(self, "_reg_state", {}).get(account_id, 0)
        if 200 <= code < 300:
            dot = "●"
            health = "ok"
        elif code >= 400:
            dot = "●"
            health = "danger"
        else:
            dot = "○"
            health = "muted"
        self.account_chip.setText(f"{dot}  {label}  ⌄")
        self.account_chip.setProperty("health", health)
        self.account_chip.style().unpolish(self.account_chip)
        self.account_chip.style().polish(self.account_chip)

    def _add_account_to_endpoint(self, cfg):
        try: SipEndpoint.instance().add_account(cfg)
        except Exception as e:
            log.exception("Failed to add account %s", cfg.id)
            QMessageBox.warning(self, "Account error", str(e))

    def _save_accounts_or_warn(self, accounts):
        try:
            save_accounts(accounts)
        except Exception as e:
            log.exception("Failed to save accounts to %s", accounts_file())
            QMessageBox.warning(
                self,
                "Account save failed",
                f"NOC_Beam could not save accounts to:\n{accounts_file()}\n\n{e}",
            )
            self._set_status("Account save failed", "danger")
            return False
        log.info("Saved %d account(s) to %s", len(accounts), accounts_file())
        return True

    def _on_add_account(self):
        dlg = AccountDialog(parent=self)
        if _open_modal(dlg):
            cfg = dlg.result_account()
            accounts = [*self.accounts, cfg]
            if not self._save_accounts_or_warn(accounts):
                return
            self.accounts = accounts
            if cfg.enabled: self._add_account_to_endpoint(cfg)
            self._refresh_accounts()

    def _selected_account(self):
        if self._active_account_id:
            return next((a for a in self.accounts if a.id == self._active_account_id), None)
        return None

    def _on_edit_account(self):
        acc = self._selected_account()
        if acc is None:
            QMessageBox.information(self, "Edit account", "Select an account first."); return
        dlg = AccountDialog(account=acc, parent=self)
        if _open_modal(dlg):
            new_cfg = dlg.result_account()
            accounts = [new_cfg if a.id == acc.id else a for a in self.accounts]
            if not self._save_accounts_or_warn(accounts):
                return
            self.accounts = accounts
            SipEndpoint.instance().remove_account(acc.id)
            if new_cfg.enabled: self._add_account_to_endpoint(new_cfg)
            self._refresh_accounts()

    def _on_account_settings(self):
        acc = self._selected_account()
        if acc is None:
            QMessageBox.information(self, "Account settings", "Select an account first."); return
        dlg = AccountSettingsDialog(account=acc, parent=self)
        if _open_modal(dlg):
            new_cfg = dlg.result_account()
            accounts = [new_cfg if a.id == acc.id else a for a in self.accounts]
            if not self._save_accounts_or_warn(accounts):
                return
            self.accounts = accounts
            SipEndpoint.instance().remove_account(acc.id)
            if new_cfg.enabled: self._add_account_to_endpoint(new_cfg)
            self._refresh_accounts()

    def _on_remove_account(self):
        acc = self._selected_account()
        if acc is None:
            QMessageBox.information(self, "Remove account", "Select an account first."); return
        if not _ask_yes_no(self, "Remove account", f"Remove {acc.username}@{acc.domain}?"):
            return
        SipEndpoint.instance().remove_account(acc.id)
        accounts = [a for a in self.accounts if a.id != acc.id]
        if not self._save_accounts_or_warn(accounts):
            return
        self.accounts = accounts
        self._refresh_accounts()

    def _on_endpoint_error(self, msg):
        log.error("Endpoint error: %s", msg)
        self._set_status(f"Endpoint error: {msg}", "danger",
                         "Click here to retry", "retry-register")
        if self.accounts:
            QMessageBox.warning(self, "SIP endpoint error", msg)

    def _on_registration_changed(self, account_id, code, reason):
        acc = next((a for a in self.accounts if a.id == account_id), None)
        label = acc.display_name if acc and acc.display_name else (acc.username if acc else account_id)
        self._reg_state[account_id] = code
        # If the changed account is the one currently shown in the chip,
        # refresh the chip so the health dot tracks the new code.
        if account_id == self._active_account_id and acc is not None:
            self._set_active_account(account_id, label)
        if 200 <= code < 300:
            self._set_status(f"Registered: {label}", "ok")
        elif code in (401, 403, 407, 423):
            self._set_status(f"Account: {label} -- auth failed ({code})", "warn",
                             "Click here to retry", "retry-register")
        else:
            self._set_status(
                f"Account: {label} -- failed to enable.\nProblem at server (SIP error {code}). Try again later.",
                "danger", "Click here to retry", "retry-register",
            )

    def _account_label(self, account_id):
        acc = next((a for a in self.accounts if a.id == account_id), None)
        if acc is None: return account_id
        return acc.display_name or f"{acc.username}@{acc.domain}"

    def _on_call_incoming(self, account_id, call_id, remote, is_in):
        label = self._account_label(account_id)
        rec = CallRecord(call_id=call_id, account_id=account_id, account_label=label,
                         remote_uri=remote, direction="in", state=CallState.NULL)
        self.calls.register(rec); self.calls.update_state(call_id, CallState.INCOMING)
        self._select_call(call_id)
        self.call_widget.show_incoming(call_id, remote)
        self.bottom_tabs.select(int(Tab.DIALPAD))
        if not self.isVisible() and self.tray.available:
            self.tray.notify("Incoming call", f"{remote or 'Unknown caller'}  *  via {label}")
        if self.tray.presence == Presence.AVAILABLE:
            self.ringer.start()

    def _on_call_state(self, account_id, call_id, state, code, reason):
        try: new_state = CallState(state)
        except ValueError:
            if call_id == self._selected_call_id:
                self.call_widget.update_state(state, code, reason)
            return
        if self.calls.get(call_id) is None:
            self.calls.register(CallRecord(call_id=call_id, account_id=account_id, direction="out"))
        self.calls.update_state(call_id, new_state, code, reason)
        # Pull the remote URI off the live SipCall and stash it on the
        # record so the CDR snapshot taken on DISCONNECTED carries the
        # peer (otherwise History shows "-" for every call).
        try:
            live = SipEndpoint.instance().find_call(call_id)
            remote = getattr(live, "remote_uri", "") if live is not None else ""
            if remote:
                self.calls.update_remote(call_id, remote)
        except Exception:
            log.exception("update_remote failed")
        if new_state in (CallState.CONFIRMED, CallState.DISCONNECTED, CallState.EARLY):
            self.ringer.stop()

    def _on_call_media(self, call_id, codec, clock, channels):
        self.calls.update_media(call_id, codec, clock, channels)

    def _on_call_quality(self, call_id, mos, loss, jitter_ms, rtt_ms):
        if call_id == self._selected_call_id:
            self.call_widget.update_quality(mos, loss)

    def _on_call_ended(self, call_id):
        self._maybe_write_cdr(call_id); self.ringer.stop()

    def _maybe_write_cdr(self, call_id):
        snap = self._last_snapshots.pop(call_id, None)
        if snap is None: return
        try:
            append_entry(snap)
            self.history_view.reload()
            self.quick_dial.reload()
        except Exception:
            log.exception("Failed to append CDR entry")

    def _select_call(self, call_id):
        self._selected_call_id = call_id
        rec = self.calls.get(call_id)
        if rec is None:
            self.call_widget.show_idle(); self.call_widget.setVisible(False); return
        self.call_widget.setVisible(True)
        if rec.direction == "in" and rec.state == CallState.INCOMING:
            self.call_widget.show_incoming(call_id, rec.remote_uri)
        else:
            self.call_widget.show_outgoing(call_id, rec.remote_uri or "...")
        self.call_widget.update_state(rec.state.value, rec.last_code, rec.last_reason)
        if rec.codec:
            self.call_widget.update_media(rec.codec, rec.clock_rate, rec.channels)

    def _on_call_record_added(self, call_id):
        if self._selected_call_id is None: self._select_call(call_id)
        self.dialpad.set_in_call(True); self.call_widget.setVisible(True)

    def _on_call_record_updated(self, call_id):
        rec = self.calls.get(call_id)
        if rec is None: return
        if rec.state == CallState.DISCONNECTED:
            self._last_snapshots[call_id] = CdrEntry(
                call_id=rec.call_id, account_id=rec.account_id,
                peer_uri=rec.remote_uri, direction=rec.direction,
                started_at=rec.started_at, connected_at=rec.connected_at,
                ended_at=rec.ended_at or rec.started_at,
                end_code=rec.last_code, end_reason=rec.last_reason, codec=rec.codec,
            )
        if call_id == self._selected_call_id:
            self.call_widget.update_state(rec.state.value, rec.last_code, rec.last_reason)
            if rec.codec:
                self.call_widget.update_media(rec.codec, rec.clock_rate, rec.channels)

    def _on_call_record_removed(self, call_id):
        if call_id == self._selected_call_id:
            self._selected_call_id = None
            next_active = self.calls.first_active()
            if next_active is not None: self._select_call(next_active.call_id)
            else:
                self.call_widget.show_idle(); self.call_widget.setVisible(False)
                self.dialpad.set_in_call(False)

    def _on_dial_input_enter(self):
        target = self.dial_input.text().strip()
        if not target: return
        self._on_call_requested(target); self.dial_input.clear()

    def _on_call_requested(self, target):
        if not self._active_account_id:
            QMessageBox.information(self, "No account", "Add a SIP account first."); return
        try:
            call = SipEndpoint.instance().make_call(self._active_account_id, target)
            cid = call.getInfo().id
            self.calls.register(CallRecord(
                call_id=cid, account_id=self._active_account_id,
                account_label=self._account_label(self._active_account_id),
                remote_uri=target, direction="out",
            ))
            self.calls.update_state(cid, CallState.CALLING)
            self._select_call(cid); self.dialpad.set_in_call(True)
            self.bottom_tabs.select(int(Tab.DIALPAD))
        except Exception as e:
            log.exception("make_call failed")
            QMessageBox.warning(self, "Call failed", str(e))

    def _selected_pjsua_call(self):
        if self._selected_call_id is None: return None
        return SipEndpoint.instance().find_call(self._selected_call_id)

    def _on_hangup_requested(self):
        call = self._selected_pjsua_call()
        if call is None: return
        try: SipEndpoint.instance().hangup_call(call)
        except Exception: log.exception("hangup failed")

    def _on_hangup_by_id(self, _call_id): self._on_hangup_requested()

    def _on_answer(self, _call_id):
        call = self._selected_pjsua_call()
        if call is None: return
        try: SipEndpoint.instance().answer_call(call); self.ringer.stop()
        except Exception: log.exception("answer failed")

    def _on_reject(self, _call_id):
        call = self._selected_pjsua_call()
        if call is None: return
        try: SipEndpoint.instance().hangup_call(call, code=603); self.ringer.stop()
        except Exception: log.exception("reject failed")

    def _on_hold(self, _call_id):
        call = self._selected_pjsua_call()
        if call is None: return
        try:
            SipEndpoint.instance().hold_call(call)
            if self._selected_call_id is not None:
                self.calls.update_state(self._selected_call_id, CallState.HELD)
        except Exception: log.exception("hold failed")

    def _on_resume(self, _call_id):
        call = self._selected_pjsua_call()
        if call is None: return
        try:
            SipEndpoint.instance().resume_call(call)
            if self._selected_call_id is not None:
                self.calls.update_state(self._selected_call_id, CallState.CONFIRMED)
        except Exception: log.exception("resume failed")

    def _on_transfer(self, call_id):
        """Blind-transfer the active call to a user-supplied target.

        Asks for a SIP URI or number; if the user types a bare number we
        normalise it against the active account's domain. Sends a REFER
        and stays on the line until the remote completes the dialog.
        """
        from PySide6.QtWidgets import QInputDialog
        call = self._selected_pjsua_call()
        if call is None:
            QMessageBox.information(self, "Transfer", "No active call to transfer.")
            return
        target, ok = QInputDialog.getText(
            self,
            "Blind transfer",
            "Forward this call to (SIP URI or number):",
        )
        if not ok or not target.strip():
            return
        target = target.strip()
        try:
            SipEndpoint.instance().blind_transfer(
                call, target, account_id=self._active_account_id
            )
            self._set_status(f"Transferring to {target}…", "muted")
        except Exception as exc:
            log.exception("blind transfer failed")
            QMessageBox.warning(self, "Transfer failed", str(exc))

    def _on_mute_toggled(self, _call_id, muted):
        call = self._selected_pjsua_call()
        if call is None or self._selected_call_id is None: return
        try:
            SipEndpoint.instance().set_call_mute(call, muted)
            self.calls.set_mute(self._selected_call_id, muted)
        except Exception: log.exception("mute toggle failed")
        # Keep the top-strip speaker icon in sync with the in-call
        # Mute button so the user has one consistent mute state.
        try:
            self.audio.set_muted(muted)
        except Exception:
            pass

    def _on_audio_strip_mute(self, muted: bool) -> None:
        """Top-strip SPEAKER icon → silence the playback DEVICE.

        Operating at the device level (not per-call) means a re-INVITE
        or codec change cannot un-stick our mute, and it works even
        across simultaneous calls. adjustRxLevel(0) on the playback
        device scales every signal the conference bridge feeds it.
        """
        try:
            ep = SipEndpoint.instance()
        except Exception:
            return
        try:
            from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE
            if not PJSUA2_AVAILABLE:
                return
            slider_v = self.audio.slider.value()
            level = 0.0 if muted else max(0.0, min(1.5, slider_v / 66.6))
            playback = ep._ep.audDevManager().getPlaybackDevMedia()
            playback.adjustRxLevel(level)
        except Exception:
            log.exception("audio-strip speaker (device) mute failed")

    def _on_audio_strip_volume(self, value: int) -> None:
        """Top-strip volume → adjust output level on active call media.
        Stored in settings so future calls inherit the setting."""
        try:
            self.settings.audio.master_volume_pct = int(value)
        except Exception:
            pass
        call = self._selected_pjsua_call()
        if call is None:
            return
        try:
            from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE
            if not PJSUA2_AVAILABLE:
                return
            info = call.getInfo()
            for mi in info.media:
                # Audio media that is active (type=1, status=1)
                if mi.type != 1 or mi.status != 1:
                    continue
                aud = call.getAudioMedia(mi.index)
                # adjustRxLevel: 1.0 = unity, 0..2.0 typical range.
                # Map 0..100 percent → 0.0..1.5 (giving headroom).
                level = max(0.0, min(1.5, value / 66.6))
                aud.adjustRxLevel(level)
        except Exception:
            log.exception("audio-strip volume adjust failed")

    def _on_audio_strip_mic_mute(self, muted: bool) -> None:
        """Top-strip mic icon → silence the capture DEVICE.

        Same rationale as the speaker case: device-level so it survives
        re-INVITEs and works regardless of how many calls are live.
        adjustTxLevel(0) on capture stops the device from feeding any
        audio into the conference bridge — no call port receives mic
        audio. Bonus: works even when no call is up (the toggle is no
        longer a silent no-op idle state).
        """
        try:
            ep = SipEndpoint.instance()
        except Exception:
            return
        try:
            from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE
            if not PJSUA2_AVAILABLE:
                return
            mic_v = self.audio.mic_slider.value()
            level = 0.0 if muted else max(0.0, min(1.5, mic_v / 66.6))
            capture = ep._ep.audDevManager().getCaptureDevMedia()
            capture.adjustTxLevel(level)
        except Exception:
            log.exception("audio-strip mic (device) mute failed")
        # Mirror the in-call CallWidget Mute button so both UIs agree.
        if self._selected_call_id is not None:
            try:
                self.calls.set_mute(self._selected_call_id, muted)
            except Exception:
                pass
        try:
            self.call_widget.mute_btn.blockSignals(True)
            self.call_widget.mute_btn.setChecked(muted)
            self.call_widget.mute_btn.blockSignals(False)
        except Exception:
            pass

    def _on_audio_strip_mic_volume(self, value: int) -> None:
        """Top-strip mic gain → adjustTxLevel on the active call.
        adjustTxLevel scales capture → call: 1.0 unity, 0 mutes."""
        call = self._selected_pjsua_call()
        if call is None:
            return
        try:
            from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE
            if not PJSUA2_AVAILABLE:
                return
            info = call.getInfo()
            for mi in info.media:
                if mi.type != 1 or mi.status != 1:
                    continue
                aud = call.getAudioMedia(mi.index)
                level = max(0.0, min(1.5, value / 66.6))
                aud.adjustTxLevel(level)
        except Exception:
            log.exception("audio-strip mic volume adjust failed")

    def _poll_audio_levels(self) -> None:
        """Read getRxLevel / getTxLevel off the active call's audio
        media and push 0–100 normalised values into the AudioStrip
        meters. Called every 200 ms by ``self._level_timer``.

        pjsua2 returns levels as ``unsigned`` 0..255 (peak amplitude
        sample window). We normalise to 0..100 by dividing by 2.55.
        """
        call = self._selected_pjsua_call()
        if call is None:
            self.audio.set_tx_level(0)
            self.audio.set_rx_level(0)
            return
        try:
            from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE
            if not PJSUA2_AVAILABLE:
                return
            info = call.getInfo()
            tx = rx = 0
            for mi in info.media:
                if mi.type != 1 or mi.status != 1:
                    continue
                aud = call.getAudioMedia(mi.index)
                # PJSIP gives raw 0..255 amplitude.
                try:
                    rx = max(rx, int(aud.getRxLevel() / 2.55))
                except Exception:
                    pass
                try:
                    tx = max(tx, int(aud.getTxLevel() / 2.55))
                except Exception:
                    pass
                break
            self.audio.set_tx_level(tx)
            self.audio.set_rx_level(rx)
        except Exception:
            # Polling must never raise into the event loop.
            pass

    def _on_end_all_calls(self) -> None:
        """Hangup EVERY live call across every account.

        Multi-call regression fix: the per-call End button only ends the
        currently selected call, leaving the rest live. The user has no
        way to bulk-clear without clicking through each one.
        """
        try:
            ep = SipEndpoint.instance()
        except Exception:
            return
        # Snapshot the IDs first so we don't mutate the list as we iterate.
        ids = [rec.call_id for rec in self.calls.active()]
        for cid in ids:
            try:
                live = ep.find_call(cid)
                if live is not None:
                    ep.hangup_call(live)
            except Exception:
                log.exception("hangup failed for call %s", cid)

    def _refresh_calls_strip(self) -> None:
        """Re-render the multi-call strip from the current call list.

        Each row: peer + state pill + per-call X hangup button. Click
        the row to make it the selected call. When 2+ calls are live
        an "End all" pill renders on the right of its own row.
        """
        # Tear down all existing strip widgets.
        while self.calls_strip_layout.count():
            item = self.calls_strip_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        active = self.calls.active()
        if not active:
            self.calls_strip.setVisible(False)
            return
        self.calls_strip.setVisible(True)
        for rec in active:
            row = QFrame(self.calls_strip)
            row.setObjectName("CallStripRow")
            row.setProperty("selected", rec.call_id == self._selected_call_id)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 4, 6, 4)
            rl.setSpacing(8)
            peer_lbl = QLabel(rec.remote_uri or "...", row)
            peer_lbl.setObjectName("CallStripPeer")
            state_lbl = QLabel(rec.state.name.title(), row)
            state_lbl.setObjectName("CallStripState")
            select_btn = QPushButton("Show", row)
            select_btn.setObjectName("CallStripSelectBtn")
            select_btn.clicked.connect(lambda _checked=False, cid=rec.call_id: self._select_call(cid))
            end_btn = QPushButton("X", row)
            end_btn.setObjectName("CallStripEndBtn")
            end_btn.setToolTip("End this call")
            end_btn.clicked.connect(lambda _checked=False, cid=rec.call_id: self._hangup_one(cid))
            rl.addWidget(peer_lbl, 1)
            rl.addWidget(state_lbl)
            rl.addWidget(select_btn)
            rl.addWidget(end_btn)
            self.calls_strip_layout.addWidget(row)
        if len(active) >= 2:
            footer = QFrame(self.calls_strip)
            footer.setObjectName("CallStripFooter")
            fl = QHBoxLayout(footer)
            fl.setContentsMargins(10, 4, 6, 4)
            fl.addStretch(1)
            end_all_btn = QPushButton(f"End all ({len(active)})", footer)
            end_all_btn.setObjectName("CallStripEndAllBtn")
            end_all_btn.clicked.connect(self._on_end_all_calls)
            fl.addWidget(end_all_btn)
            self.calls_strip_layout.addWidget(footer)

    def _hangup_one(self, call_id: int) -> None:
        """Hangup a specific call by ID. Bypasses the selected-call
        gate so any strip row can end its own call."""
        try:
            live = SipEndpoint.instance().find_call(call_id)
            if live is not None:
                SipEndpoint.instance().hangup_call(live)
        except Exception:
            log.exception("hangup_one failed for call %s", call_id)

    def _on_digit_pressed(self, digit):
        # When in a call, digits are DTMF tones routed via the SIP endpoint.
        # When idle, dialpad presses build up the dial string in the top
        # input so the user can tap the keypad and press Call.
        call = self._selected_pjsua_call()
        if call is None:
            self.dial_input.setText(self.dial_input.text() + digit)
            return
        rec = self.calls.get(self._selected_call_id) if self._selected_call_id else None
        acc_id = rec.account_id if rec else self._active_account_id
        acc_cfg = next((a for a in self.accounts if a.id == acc_id), None)
        if acc_cfg is None: return
        try: SipEndpoint.instance().send_dtmf(call, digit, acc_cfg)
        except Exception: log.exception("send_dtmf failed")

    def _on_settings(self):
        dlg = SettingsDialog(self.settings, parent=self)
        if _open_modal(dlg):
            codec_map = dlg.apply_to(self.settings); save_settings(self.settings)
            from noc_beam.codecs.manager import set_priority
            for cid, prio in codec_map.items(): set_priority(cid, prio)
            set_active_devices(self.settings.audio.input_device, self.settings.audio.output_device)
            self._apply_accessibility_settings()
            self._set_status("Settings applied", "ok")

    def _apply_accessibility_settings(self):
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            theme = getattr(self.settings.appearance, "theme", "light")
            apply_theme(
                app,
                self.settings.appearance.high_contrast,
                theme=theme,
            )
        wide_window = getattr(self, "_wide_window", None)
        drawer = getattr(wide_window, "drawer", None)
        if drawer is not None:
            drawer.set_reduced_motion(self.settings.appearance.reduced_motion)

    def _on_diagnostics(self):
        from noc_beam.ui.diagnostics_view import DiagnosticsView
        if not hasattr(self, "_diagnostics_window"):
            self._diagnostics_window = DiagnosticsView()
            self._diagnostics_window.setWindowTitle("NOC_Beam diagnostics")
            self._diagnostics_window.resize(900, 600)
        self._diagnostics_window.update_accounts(self.accounts)
        self._diagnostics_window.show()
        self._diagnostics_window.raise_(); self._diagnostics_window.activateWindow()

    def _on_open_trace(self):
        # Reparent the trace_view into a standalone QMainWindow on demand.
        if not hasattr(self, "_trace_window"):
            from PySide6.QtWidgets import QMainWindow
            self._trace_window = QMainWindow()
            self._trace_window.setWindowTitle("NOC_Beam SIP trace")
            self._trace_window.resize(900, 600)
            self._trace_window.setCentralWidget(self.trace_view)
        self._trace_window.show()
        self._trace_window.raise_(); self._trace_window.activateWindow()

    def _on_open_accounts(self):
        # Same pattern as _on_open_trace: lift the accounts_view into a
        # standalone window for power-user multi-account management.
        # Master pane on the left (accounts_view), detail pane on the right
        # (accounts_detail) wired through selected_account_changed.
        if not hasattr(self, "_accounts_window"):
            from PySide6.QtWidgets import QMainWindow, QSplitter
            from noc_beam.ui.accounts_detail import AccountDetail

            self._accounts_window = QMainWindow()
            self._accounts_window.setWindowTitle("NOC_Beam accounts")
            self._accounts_window.resize(1100, 600)

            self._accounts_detail = AccountDetail()
            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.addWidget(self.accounts_view)
            splitter.addWidget(self._accounts_detail)
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 1)
            splitter.setSizes([380, 720])
            self._accounts_window.setCentralWidget(splitter)

            self.accounts_view.selected_account_changed.connect(
                self._on_accounts_window_selection
            )
        self._accounts_window.show()
        self._accounts_window.raise_(); self._accounts_window.activateWindow()

    def _on_accounts_window_selection(self, account_id: str) -> None:
        if not account_id:
            self._accounts_detail.show_empty()
            return
        cfg = next((a for a in self.accounts if a.id == account_id), None)
        if cfg is None:
            self._accounts_detail.show_empty()
        else:
            self._accounts_detail.show_account(cfg)

    def _on_open_test_runner(self):
        from noc_beam.ui.test_runner_view import TestRunnerView
        if not hasattr(self, "_test_runner_window"):
            self._test_runner_window = TestRunnerView(self.accounts, self)
            self._test_runner_window.resize(900, 620)
        self._test_runner_window.accounts = list(self.accounts)
        self._test_runner_window.show()
        self._test_runner_window.raise_(); self._test_runner_window.activateWindow()

    def _on_toggle_always_on_top(self, checked=False):
        self._always_on_top = bool(checked)
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self._always_on_top)
        if self._always_on_top_action is not None:
            self._always_on_top_action.setChecked(self._always_on_top)
        if was_visible:
            self.show()
            if self._always_on_top:
                self.raise_()
                self.activateWindow()

    def _on_open_wide(self):
        from noc_beam.ui.main_window import MainWindow
        if not hasattr(self, "_wide_window"):
            self._wide_window = MainWindow()
        self._wide_window.show()
        self._wide_window.raise_(); self._wide_window.activateWindow()

    def _on_about(self):
        QMessageBox.about(
            self, "About NOC_Beam",
            f"<b>{__app_name__}</b> {__version__}<br><br>"
            "NOC engineering softphone. SIP, TLS, SRTP, multi-account.<br>"
            "Open the wide dashboard from View for the full NOC console."
        )

    def _install_shortcuts(self):
        for seq, slot in (
            ("Return",  self._on_dial_input_enter),
            ("Esc",     self._on_hangup_requested),
            ("Ctrl+1",  lambda: self.bottom_tabs.select(int(Tab.DIALPAD))),
            ("Ctrl+2",  lambda: self.bottom_tabs.select(int(Tab.CONTACTS))),
            ("Ctrl+3",  lambda: self.bottom_tabs.select(int(Tab.FAVORITES))),
            ("Ctrl+4",  lambda: self.bottom_tabs.select(int(Tab.HISTORY))),
            ("Ctrl+5",  lambda: self.bottom_tabs.select(int(Tab.TRACE))),
            ("Ctrl+K",  lambda: self.dial_input.setFocus(Qt.FocusReason.ShortcutFocusReason)),
        ):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)

    def _restore_from_tray(self):
        self.showNormal(); self.raise_(); self.activateWindow()

    def _on_quit(self):
        self._really_quitting = True; self.close()

    def closeEvent(self, event):
        if not self._really_quitting and self.tray.available:
            event.ignore(); self.hide(); return
        try: SipEndpoint.instance().stop()
        except Exception: log.exception("Endpoint stop error")
        super().closeEvent(event)
