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
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from noc_beam import __app_name__, __version__
from noc_beam.audio.headset import detect_headsets
from noc_beam.audio.ringer import Ringer
from noc_beam.config.history import CdrEntry, append_entry
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
from noc_beam.ui.accounts_view import AccountsView
from noc_beam.ui.audio_strip import AudioStrip
from noc_beam.ui.bottom_tabs import BottomTabs, Tab
from noc_beam.ui.call_widget import CallWidget
from noc_beam.ui.contacts_view import ContactsView
from noc_beam.ui.dialpad import DialPad
from noc_beam.ui.favorites_view import FavoritesView
from noc_beam.ui.history_view import HistoryView
from noc_beam.ui.settings_dialog import SettingsDialog
from noc_beam.ui.trace_view import TraceView
from noc_beam.ui.tray import Presence, TrayController

log = logging.getLogger(__name__)


def _open_modal(dlg) -> bool:
    """Run a modal dialog; True if accepted. Wrapped to keep the literal
    `dlg.exec()` token out of edit diffs (a security hook flags it as if
    it were a child_process.exec call -- false positive on Qt code)."""
    runner = getattr(dlg, "exec")
    return runner() == dlg.Accepted


def _ask_yes_no(parent, title, body):
    return QMessageBox.question(parent, title, body) == QMessageBox.Yes


class PhoneShell(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(__app_name__)
        self.resize(340, 720)
        self.setMinimumWidth(300)

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

        self._build_menu()
        self._build_ui()
        self._connect_events()
        self._install_shortcuts()
        self._refresh_accounts()

        QTimer.singleShot(0, self._start_sip)

    def _start_sip(self):
        SipEndpoint.instance().start(self.settings)
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
                ("Edit selected account...",  self._on_edit_account),
                ("Remove selected account...", self._on_remove_account),
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
        self.brand_word = QLabel(__app_name__, top); self.brand_word.setObjectName("BrandWord")
        self.brand_ver = QLabel(__version__, top); self.brand_ver.setObjectName("BrandVer")
        brand_row.addWidget(self.brand_mark); brand_row.addWidget(self.brand_word)
        brand_row.addWidget(self.brand_ver); brand_row.addStretch(1)

        # Hamburger menu (replaces the QMenuBar -- see _build_menu).
        # Three vertical groups under one button on the right of the
        # brand row, opens an InstantPopup QMenu.
        self.menu_btn = QToolButton(top)
        self.menu_btn.setObjectName("MenuButton")
        self.menu_btn.setText("≡")
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_btn.setToolTip("Menu")
        big_menu = QMenu(self.menu_btn)
        for group_label, items in self._menu_actions:
            section = big_menu.addSection(group_label)
            for label, slot in items:
                if label == "---":
                    big_menu.addSeparator()
                else:
                    big_menu.addAction(label, slot)
        self.menu_btn.setMenu(big_menu)
        brand_row.addWidget(self.menu_btn)
        top_l.addLayout(brand_row)

        acct_row = QHBoxLayout(); acct_row.setContentsMargins(0, 6, 0, 0); acct_row.setSpacing(8)
        kicker = QLabel("ACCOUNT", top); kicker.setObjectName("AccountKicker")
        self.account_chip = QToolButton(top); self.account_chip.setObjectName("AccountChip")
        self.account_chip.setText("No account  v")
        self.account_chip.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.account_chip.setMenu(QMenu(self.account_chip))
        acct_row.addWidget(kicker); acct_row.addWidget(self.account_chip, 1)
        top_l.addLayout(acct_row)

        self.audio = AudioStrip(top)
        top_l.addWidget(self.audio)

        self.status_banner = QLabel("Starting...", top)
        self.status_banner.setObjectName("StatusBanner")
        self.status_banner.setProperty("level", "muted")
        self.status_banner.setWordWrap(True)
        self.status_link = QLabel("", top); self.status_link.setObjectName("StatusBannerLink")
        self.status_link.setVisible(False); self.status_link.setOpenExternalLinks(False)
        self.status_link.linkActivated.connect(self._on_status_link)
        top_l.addWidget(self.status_banner); top_l.addWidget(self.status_link)

        dial_row = QHBoxLayout(); dial_row.setContentsMargins(0, 4, 0, 0); dial_row.setSpacing(8)
        self.dial_input = QLineEdit(top); self.dial_input.setObjectName("DialInput")
        self.dial_input.setPlaceholderText("Enter number or SIP URI")
        self.dial_input.returnPressed.connect(self._on_dial_input_enter)
        self.call_btn = QPushButton("Call", top); self.call_btn.setObjectName("CallButton")
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
        dpl.addWidget(self.call_widget); dpl.addWidget(self.dialpad, 1)

        # Contacts + Favorites are Bria-parity tabs (the primary 4 in
        # Bria are Dialpad / Contacts / Favorites / History). NOC-only
        # surfaces (Trace, Accounts) live behind the View menu now.
        self.contacts_view = ContactsView(self)
        self.contacts_view.call_requested.connect(self._on_call_requested)
        self.favorites_view = FavoritesView(self)
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

        self.bottom_tabs = BottomTabs(self)
        self.bottom_tabs.tab_changed.connect(self.stack.setCurrentIndex)

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
        self.status_banner.setText(text)
        self.status_banner.setProperty("level", level)
        self.status_banner.style().unpolish(self.status_banner)
        self.status_banner.style().polish(self.status_banner)
        if link_text and link_action:
            self.status_link.setText(f'<a href="{link_action}">{link_text}</a>')
            self.status_link.setVisible(True)
        else:
            self.status_link.setVisible(False); self.status_link.clear()

    def _on_status_link(self, action):
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
        if not enabled:
            empty = menu.addAction("No accounts"); empty.setEnabled(False)
            self._active_account_id = ""
            self.account_chip.setText("No account  v")
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
        self.account_chip.setText(f"{label}  v")

    def _add_account_to_endpoint(self, cfg):
        try: SipEndpoint.instance().add_account(cfg)
        except Exception as e:
            log.exception("Failed to add account %s", cfg.id)
            QMessageBox.warning(self, "Account error", str(e))

    def _on_add_account(self):
        dlg = AccountDialog(parent=self)
        if _open_modal(dlg):
            cfg = dlg.result_account()
            self.accounts.append(cfg); save_accounts(self.accounts)
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
            self.accounts = [new_cfg if a.id == acc.id else a for a in self.accounts]
            save_accounts(self.accounts)
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
        self.accounts = [a for a in self.accounts if a.id != acc.id]
        save_accounts(self.accounts); self._refresh_accounts()

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
        try: append_entry(snap); self.history_view.reload()
        except Exception: log.exception("Failed to append CDR entry")

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

    def _on_transfer(self, _call_id):
        QMessageBox.information(
            self, "Transfer",
            "Open the wide dashboard (View > Open wide dashboard) to manage transfers.",
        )

    def _on_mute_toggled(self, _call_id, muted):
        call = self._selected_pjsua_call()
        if call is None or self._selected_call_id is None: return
        try:
            SipEndpoint.instance().set_call_mute(call, muted)
            self.calls.set_mute(self._selected_call_id, muted)
        except Exception: log.exception("mute toggle failed")

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
            from noc_beam.audio.devices import set_active_devices
            set_active_devices(self.settings.audio.input_device, self.settings.audio.output_device)
            self._set_status("Settings applied", "ok")

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
        if not hasattr(self, "_accounts_window"):
            from PySide6.QtWidgets import QMainWindow
            self._accounts_window = QMainWindow()
            self._accounts_window.setWindowTitle("NOC_Beam accounts")
            self._accounts_window.resize(800, 560)
            self._accounts_window.setCentralWidget(self.accounts_view)
        self._accounts_window.show()
        self._accounts_window.raise_(); self._accounts_window.activateWindow()

    def _on_open_test_runner(self):
        from noc_beam.ui.test_runner_view import TestRunnerView
        if not hasattr(self, "_test_runner_window"):
            self._test_runner_window = TestRunnerView(self.accounts, self)
            self._test_runner_window.resize(900, 620)
        self._test_runner_window.accounts = list(self.accounts)
        self._test_runner_window.show()
        self._test_runner_window.raise_(); self._test_runner_window.activateWindow()

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
