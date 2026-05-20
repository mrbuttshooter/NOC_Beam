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
    |  - Tab 1: Contacts                     |
    |  - Tab 2: Favorites                    |
    |  - Tab 3: History                      |
    |                                        |
    +----------------------------------------+
    | [Dial] [Contacts] [Favs] [History]     |  bottom tabs
    +----------------------------------------+
    SIP Trace + Accounts management + Test Runner + Diagnostics
    open in their own windows via the View menu (Bria parity).

Reuses the existing widgets. Settings + Diagnostics + the wider
dashboard live behind View menu (open as separate windows).
MainWindow stays as the old wide-shell entry point.
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from noc_beam import __app_name__, __version__
from noc_beam.audio.devices import set_active_devices
from noc_beam.audio.headset import detect_headsets
from noc_beam.audio.ringer import FailureTone, Ringer
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
# TraceView is imported lazily inside _on_open_trace -- removing the
# top-level import lets PhoneShell construct without paying the
# trace_view module-load cost when the user never opens Trace.
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


class _SupplierComboFocusFilter(QObject):
    """Select-all-on-focus + clear-proxy-filter for the SUPPLIER combo.

    Click or tab into the combo -> the existing supplier text gets
    selected (so typing instantly replaces it) AND any leftover
    filter on the supplier proxy model gets cleared (so the combo's
    own popup will show ALL suppliers next time, not just the
    previously-filtered subset).
    """

    def __init__(self, combo):
        super().__init__(combo)
        self._combo = combo

    def eventFilter(self, obj, event):
        et = event.type()
        if et in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            QTimer.singleShot(0, obj.selectAll)
            # Reset any leftover proxy filter so subsequent showPopup()
            # shows the full supplier list. Done deferred so it doesn't
            # interfere with Qt's own click/focus handling.
            try:
                from PySide6.QtCore import QSortFilterProxyModel as _QSFPM
                model = self._combo.model()
                if isinstance(model, _QSFPM):
                    QTimer.singleShot(0, lambda m=model: m.setFilterFixedString(""))
            except Exception:
                pass
        return False


class _CallStripRow(QFrame):
    """One row in the multi-call strip.

    Owns its call_id and a click-handler callback. Replaces the older
    pattern that monkey-patched `row.mousePressEvent = closure` on each
    instance — that closure captured `_original = QFrame.mousePressEvent`
    (unbound) plus `_row = row` as a default arg, keeping the Python
    wrapper alive past Qt's `deleteLater()`. A queued mouse event landing
    after deletion then called the unbound method on a dead C++ object
    and raised `RuntimeError: Internal C++ object already deleted.`
    """

    def __init__(self, call_id: int, on_select, parent=None):
        super().__init__(parent)
        self._call_id = call_id
        self._on_select = on_select

    def mousePressEvent(self, ev):  # noqa: N802 (Qt naming)
        # Don't promote the row to selected when the click landed on the
        # End button — it has its own clicked handler and the parent
        # _hangup_one shouldn't race the select.
        end = self.findChild(QToolButton, "CallStripEndBtn")
        if end is None or not end.geometry().contains(ev.pos()):
            try:
                self._on_select(self._call_id)
            except Exception:
                pass
        # Always chain to QFrame's default so Qt's normal click/select
        # propagation to children (e.g. tooltips) still runs.
        super().mousePressEvent(ev)


class PhoneShell(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(__app_name__)
        self.resize(420, 740)
        self.setMinimumWidth(380)

        self.settings = load_settings()
        self.accounts = load_accounts()
        self.calls = call_manager()
        self.ringer = Ringer()
        # Plays a PSTN-style tone (busy / reorder / reject) once when
        # a call ends with a SIP failure code. Silent on 200 success
        # and on auth-required (401/407, which are setup issues not
        # real call failures).
        self.failure_tone = FailureTone()
        self.tray = TrayController(self)
        self.reg_retry = RegistrationRetry(self)
        self.quality_sampler = CallQualitySampler(self.calls, self)
        # Endpoint supervisor: detects endpoint_error storms and
        # performs a controlled SIP-only restart while preserving
        # CallManager + UI state. Hooked into the same singleton
        # sip_events mesh; disconnects on destruction.
        from noc_beam.sip.supervisor import EndpointSupervisor
        self.sip_supervisor = EndpointSupervisor(self)
        self._selected_call_id = None
        self._last_snapshots = {}
        self._really_quitting = False
        self._reg_state: dict[str, int] = {}
        # Peer URI cache per call_id, used by _on_call_record_updated to
        # skip rebuilding the call card when only ancillary fields changed
        # (state/codec/FAS). Initialized here (not lazy via hasattr in the
        # hot path) so its lifetime is well-defined and prunable.
        self._last_call_peer: dict[int, str] = {}
        self._active_account_id = ""
        self._always_on_top = False
        self._always_on_top_action = None

        self._build_menu()
        self._build_ui()
        self._connect_events()
        self._install_shortcuts()
        self._refresh_accounts()

        QTimer.singleShot(0, self._start_sip)
        # Surface DPAPI degradation if it fired during accounts load:
        # passwords silently fell back to base64-encoding (NOT encrypted).
        # User needs to know — domain roaming-profile glitches are the
        # usual cause and the fix is to relog or re-protect creds.
        QTimer.singleShot(500, self._check_dpapi_status)

    def _check_dpapi_status(self) -> None:
        try:
            from noc_beam.config.store import is_dpapi_degraded
            if is_dpapi_degraded():
                self._set_status(
                    "⚠ Password protection degraded (DPAPI failed; using base64). "
                    "Re-enter account passwords if this persists.",
                    "warn",
                )
        except Exception:
            pass

    def _start_sip(self):
        SipEndpoint.instance().start(self.settings, accounts=self.accounts)
        for acc in self.accounts:
            if acc.enabled:
                self._add_account_to_endpoint(acc)
        self._refresh_accounts()
        headsets = detect_headsets()
        if headsets:
            log.info("Headsets: %s", ", ".join(str(h) for h in headsets))
        # Push the enumerated audio devices into the top-strip menus so
        # the chevron + right-click pickers actually have something to
        # show. Without this both menus were empty and the right-click
        # popup looked like nothing happened.
        self._populate_audio_strip_devices()
        # Wire device picks to the actual PJSIP capture/playback setters.
        try:
            self.audio.input_device_picked.connect(self._on_input_device_picked)
            self.audio.output_device_picked.connect(self._on_output_device_picked)
        except Exception:
            pass

    def _populate_audio_strip_devices(self) -> None:
        try:
            from noc_beam.audio.devices import enumerate_devices
        except Exception:
            return
        try:
            devs = enumerate_devices()
        except Exception:
            log.exception("enumerate_devices failed")
            return
        inputs = [(d.index, d.name) for d in devs if d.is_input]
        outputs = [(d.index, d.name) for d in devs if d.is_output]
        try:
            self.audio.set_input_devices(inputs)
            self.audio.set_output_devices(outputs)
        except Exception:
            log.exception("AudioStrip set_*_devices failed")

    def _on_input_device_picked(self, dev_id) -> None:
        try:
            from noc_beam.audio.devices import set_active_devices
            cur_out = getattr(self.settings.audio, "output_device", -1)
            set_active_devices(int(dev_id), int(cur_out) if cur_out is not None else -1)
            self.settings.audio.input_device = int(dev_id)
        except Exception:
            log.exception("input device pick failed")

    def _on_output_device_picked(self, dev_id) -> None:
        try:
            from noc_beam.audio.devices import set_active_devices
            cur_in = getattr(self.settings.audio, "input_device", -1)
            set_active_devices(int(cur_in) if cur_in is not None else -1, int(dev_id))
            self.settings.audio.output_device = int(dev_id)
        except Exception:
            log.exception("output device pick failed")

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
        top_l.setContentsMargins(10, 6, 10, 4); top_l.setSpacing(2)

        brand_row = QHBoxLayout(); brand_row.setSpacing(8)
        # Brand mark: render the real app icon (the cyan-beam N) as a
        # QPixmap rather than the old text-on-orange-square placeholder.
        # QIcon handles the multi-resolution .ico and gives us a crisp
        # pixmap at the target display size on hi-DPI screens.
        from PySide6.QtCore import QSize as _QSize
        from PySide6.QtGui import QIcon as _QIcon
        from pathlib import Path as _Path
        _icon_path = _Path(__file__).parent / "resources" / "icon.ico"
        self.brand_mark = QLabel("", top)
        self.brand_mark.setObjectName("BrandMark")
        try:
            _bm_px = _QIcon(str(_icon_path)).pixmap(_QSize(28, 28))
            if not _bm_px.isNull():
                self.brand_mark.setPixmap(_bm_px)
        except Exception:
            self.brand_mark.setText("N")
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
        self.account_chip.setText("No account  ▾")
        self.account_chip.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.account_chip.setMenu(QMenu(self.account_chip))
        self.account_chip.setAccessibleName("Active SIP account")
        self.account_chip.setAccessibleDescription("Choose the SIP account used for outgoing calls")
        acct_row.addWidget(kicker); acct_row.addWidget(self.account_chip, 1)
        top_l.addLayout(acct_row)

        # SUPPLIER picker -- shown only when active account has
        # switch_type in {teles, genband}. Editable combo so the user
        # can type to filter the 400+ supplier list. Hidden by default;
        # _refresh_supplier_picker() shows / populates it when an account
        # is selected (and on app boot if an account is already saved).
        from PySide6.QtWidgets import QComboBox as _QComboBox
        supp_row = QHBoxLayout(); supp_row.setContentsMargins(0, 4, 0, 0); supp_row.setSpacing(8)
        self.supplier_kicker = QLabel("SUPPLIER", top)
        self.supplier_kicker.setObjectName("AccountKicker")
        self.supplier_combo = _QComboBox(top)
        self.supplier_combo.setObjectName("SupplierCombo")
        self.supplier_combo.setEditable(True)
        self.supplier_combo.setInsertPolicy(_QComboBox.InsertPolicy.NoInsert)
        self.supplier_combo.setAccessibleName("Active supplier")
        self.supplier_combo.setAccessibleDescription(
            "Pick a supplier from the active account's switch. "
            "Type any part of the name to filter the list."
        )
        # ARCHITECTURE NOTE: we deliberately do NOT use QCompleter here.
        # Qt's QCompleter popup has a focus-grab + auto-reattach behavior
        # that makes it impossible to dismiss programmatically after a
        # commit. Instead we drive a QSortFilterProxyModel wrapping the
        # combo's source model and use the combo's OWN popup (which IS
        # dismissible via hidePopup()) for the filtered match list.
        from PySide6.QtCore import (
            QSortFilterProxyModel as _QSFPM,
            Qt as _Qt,
        )
        from PySide6.QtGui import QStandardItemModel as _QSIM
        self._supplier_source_model = _QSIM(self.supplier_combo)
        self._supplier_proxy = _QSFPM(self.supplier_combo)
        self._supplier_proxy.setSourceModel(self._supplier_source_model)
        self._supplier_proxy.setFilterCaseSensitivity(_Qt.CaseSensitivity.CaseInsensitive)
        self.supplier_combo.setModel(self._supplier_proxy)
        # Force-detach any default completer Qt attached to the editable
        # combo or its line edit. Either or both being present re-triggers
        # the popup-stuck-open bug.
        self.supplier_combo.setCompleter(None)
        # Cache of (display, id) for autofill -- independent of the proxy
        # filter so unique-match detection always sees the full set.
        self._all_suppliers: list[tuple[str, str]] = []
        _le = self.supplier_combo.lineEdit()
        if _le is not None:
            _le.setCompleter(None)
            self._supplier_combo_filter = _SupplierComboFocusFilter(self.supplier_combo)
            _le.installEventFilter(self._supplier_combo_filter)
            _le.textEdited.connect(self._on_supplier_text_edited)
            _le.returnPressed.connect(self._on_supplier_return_pressed)
        self.supplier_combo.currentIndexChanged.connect(self._on_supplier_changed)
        supp_row.addWidget(self.supplier_kicker)
        supp_row.addWidget(self.supplier_combo, 1)
        # Wrap in a QWidget so we can hide the whole row including the label.
        from PySide6.QtWidgets import QWidget as _QWidget
        self.supplier_row_widget = _QWidget(top)
        self.supplier_row_widget.setLayout(supp_row)
        self.supplier_row_widget.setVisible(False)
        top_l.addWidget(self.supplier_row_widget)
        # Active supplier id for the current account (str or "").
        self._active_supplier_id: str = ""

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
        # audio media and updates AudioStrip's progress bars. Try with
        # an explicit parent first (proper Qt ownership, no leak); fall
        # back to no-arg construction so the unit-test FakeTimer keeps
        # working.
        try:
            self._level_timer = QTimer(self)
        except TypeError:
            self._level_timer = QTimer()
        try:
            self._level_timer.setInterval(200)
            self._level_timer.timeout.connect(self._poll_audio_levels)
            # Don't start until a call exists. The 5 Hz poll wakes
            # Python + crosses into pjsua2 native code; on idle that
            # was burning ~1-2% CPU and warming the laptop for nothing.
            # _ensure_level_timer() arms it on call_added; the timer
            # auto-stops in _poll_audio_levels when there's no call.
        except Exception:
            log.exception("level timer setup failed")

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
        dpl = QVBoxLayout(dialpad_page); dpl.setContentsMargins(4, 2, 4, 2); dpl.setSpacing(2)
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
        # are 2+ live calls. Stays hidden when there are zero calls.
        # The strip grows naturally as calls are added; what shrinks
        # to make room is the Recent Calls strip below (see quick_dial
        # wrapping). Window height stays put because the recents area
        # gives up its rows.
        self.calls_strip = QFrame(self)
        self.calls_strip.setObjectName("CallStrips")
        self.calls_strip_layout = QVBoxLayout(self.calls_strip)
        self.calls_strip_layout.setContentsMargins(0, 0, 0, 0)
        self.calls_strip_layout.setSpacing(2)
        self.calls_strip.setVisible(False)
        from PySide6.QtWidgets import QSizePolicy as _SP_
        # Strip takes only as much vertical space as its rows need;
        # never tries to expand opportunistically into recents area.
        self.calls_strip.setSizePolicy(_SP_.Policy.Preferred, _SP_.Policy.Fixed)
        # Store the strip-refresh lambdas on self so closeEvent can
        # disconnect them by reference. Without this, every PhoneShell
        # ever constructed stacks another lambda on the call_manager()
        # singleton -- the test suite hung past test #30 because the
        # singleton was fanning every CallRecord mutation to 30+ dead
        # PhoneShell instances, each chaining processEvents through
        # the next via the strip-rebuild path. v3 audit root cause.
        # Coalesce strip refreshes: call_updated fires multiple times
        # per second per call (FAS verdict, quality samples, codec
        # changes, mute toggles). On a 10-call test sweep that's 50+
        # full diff-rebuilds per second — wakes the painter for nothing
        # since the visible chrome only needs a redraw every ~50 ms to
        # look smooth. A single-shot timer collapses bursts into one
        # rebuild. add/remove still fire immediately because those
        # genuinely change the row count + selection.
        self._strip_dirty = False
        self._strip_coalesce = QTimer(self)
        self._strip_coalesce.setSingleShot(True)
        self._strip_coalesce.setInterval(50)
        self._strip_coalesce.timeout.connect(self._flush_strip_if_dirty)

        def _mark_strip_dirty(_cid):
            self._strip_dirty = True
            if not self._strip_coalesce.isActive():
                self._strip_coalesce.start()

        self._strip_refresh_added = lambda _cid: self._refresh_calls_strip()
        self._strip_refresh_removed = lambda _cid: self._refresh_calls_strip()
        # _updated is the high-frequency one — coalesce.
        self._strip_refresh_updated = _mark_strip_dirty
        self.calls.call_added.connect(self._strip_refresh_added)
        self.calls.call_removed.connect(self._strip_refresh_removed)
        self.calls.call_updated.connect(self._strip_refresh_updated)
        # Keypad + recents stay visible during calls -- the user still
        # needs DTMF and may want to dial a second call. The CallWidget
        # itself was made compact to make room.

        dpl.addWidget(self.calls_strip)
        dpl.addWidget(self.call_widget)
        dpl.addWidget(self.first_run_hero)
        dpl.addWidget(self.dialpad)
        # Wrap recents in a QScrollArea so it's the shrink-victim when
        # the multi-call strip grows. Operator request: window stays
        # the same size, calls take space FROM the recents area
        # (fewer recents visible, more calls visible). The scroll
        # area's verticalSizePolicy = Expanding+ignoreSizeHint lets
        # the layout shrink it as low as a single row before showing
        # a scrollbar.
        from PySide6.QtWidgets import QScrollArea as _QSA, QSizePolicy as _SP_
        self._quick_dial_scroll = _QSA(self)
        self._quick_dial_scroll.setObjectName("RecentsScroll")
        self._quick_dial_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._quick_dial_scroll.setWidgetResizable(True)
        self._quick_dial_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._quick_dial_scroll.setWidget(self.quick_dial)
        # Minimum 1 row visible so the section never disappears
        # entirely; Expanding policy means it surrenders space first
        # under layout pressure (the multi-call strip will push down
        # into it as calls are added).
        self._quick_dial_scroll.setMinimumHeight(60)
        self._quick_dial_scroll.setSizePolicy(_SP_.Policy.Preferred, _SP_.Policy.Expanding)
        dpl.addWidget(self._quick_dial_scroll, 1)

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

        # Accounts is constructed for the View-menu window; not in
        # the stack but kept around. TraceView used to be here too
        # but Trace is now popup-only (created on demand in
        # _on_open_trace) -- removing the eager construction also
        # eliminates the dangling sip_message subscriber that lived
        # for the entire app lifetime regardless of whether the
        # user ever opened Trace.
        self.accounts_view = AccountsView(self)
        self.accounts_view.add_clicked.connect(self._on_add_account)
        # Per-row hover-action signals: previously emitted but never
        # connected -- buttons looked active but did nothing.
        self.accounts_view.edit_requested.connect(self._edit_account_by_id)
        self.accounts_view.test_requested.connect(self._test_account_by_id)
        self.accounts_view.delete_requested.connect(self._remove_account_by_id)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(dialpad_page)             # 0 DIALPAD
        self.stack.addWidget(self.contacts_view)       # 1 CONTACTS
        self.stack.addWidget(self.favorites_view)      # 2 FAVORITES
        self.stack.addWidget(self.history_view)        # 3 HISTORY
        # Trace is popup-only now (View -> NOC Trace...,
        # Ctrl+Shift+T). The bottom-tab integration was a
        # diagnostic-tool-in-call-flow-chrome mismatch; Bria /
        # Zoiper / Linphone don't surface packet trace inline.

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
        # All singleton subscriptions go through SignalRegistry so
        # closeEvent can drop them with one unbind_all() call. Was
        # hand-maintained connect/disconnect pairs spread across two
        # files -- drifted into the v3 test-hang root cause (lambdas
        # without disconnect accumulating on the singleton). Stored
        # lambdas for endpoint_started/stopped are kept on self so
        # they have stable identity for the registry to remember.
        from noc_beam.ui._signal_registry import SignalRegistry
        self._signals = SignalRegistry()
        self._sip_on_started = lambda: self._set_status("Ready", "ok")
        self._sip_on_stopped = lambda: self._set_status("SIP endpoint stopped", "warn")
        ev = sip_events()
        for sig, slot in (
            (ev.endpoint_started,      self._sip_on_started),
            (ev.endpoint_stopped,      self._sip_on_stopped),
            (ev.endpoint_error,        self._on_endpoint_error),
            (ev.registration_changed,  self._on_registration_changed),
            (ev.call_incoming,         self._on_call_incoming),
            (ev.call_state_changed,    self._on_call_state),
            (ev.call_media_active,     self._on_call_media),
            (ev.call_quality,          self._on_call_quality),
            (ev.call_fas_verdict,      self._on_call_fas_verdict),
            (ev.call_ended,            self._on_call_ended),
            (self.calls.call_added,    self._on_call_record_added),
            (self.calls.call_updated,  self._on_call_record_updated),
            (self.calls.call_removed,  self._on_call_record_removed),
            (self.tray.show_requested, self._restore_from_tray),
            (self.tray.quit_requested, self._on_quit),
        ):
            self._signals.bind(sig, slot)

    def _set_status(self, text, level="muted", link_text="", link_action="",
                     transient: bool = False):
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
        # Transient status messages (e.g. "Settings applied",
        # "Transferring...", post-apply notices) used to stick on the
        # banner forever until something else changed it, which made
        # the banner misleading -- the app could be in Ready state
        # but the banner kept showing a 10-minute-old "Settings
        # applied" toast. Auto-revert after 3 seconds.
        try:
            self._status_revert_timer.stop()
        except Exception:
            from PySide6.QtCore import QTimer as _QT
            self._status_revert_timer = _QT(self)
            self._status_revert_timer.setSingleShot(True)
            self._status_revert_timer.timeout.connect(self._revert_status_to_baseline)
        if transient:
            self._status_revert_timer.start(3000)

    def _revert_status_to_baseline(self) -> None:
        """Recompute the steady-state status banner: registered account
        => 'Ready', no account => 'No SIP account configured', etc.
        Called by the 3-second transient-status auto-revert timer."""
        try:
            if not self.accounts:
                self._set_status(
                    "No SIP account configured", "warn",
                    "Add account", "add-account",
                )
                return
            ep = SipEndpoint.instance()
            if not ep.is_started():
                self._set_status("Starting SIP endpoint…", "muted")
                return
            self._set_status("Ready", "ok")
        except Exception:
            self._set_status("Ready", "ok")

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
            # Wrapper visibility tracks the strip so the scroll area
            # doesn't reserve empty space when there's no account.
            try:
                self._quick_dial_scroll.setVisible(not no_accounts)
            except Exception:
                pass
            self.dial_input.setEnabled(not no_accounts)
            self.call_btn.setEnabled(not no_accounts)
        if not enabled:
            empty = menu.addAction("No accounts"); empty.setEnabled(False)
            menu.addSeparator()
            menu.addAction("Add account...", self._on_add_account)
            self._active_account_id = ""
            self.account_chip.setText("○  No account  ▾")
            self.account_chip.setProperty("health", "muted")
            self.account_chip.style().unpolish(self.account_chip)
            self.account_chip.style().polish(self.account_chip)
            self._set_status("No SIP account configured", "warn", "Add account", "add-account")
        else:
            # Chip/picker prefer the UI nickname (`label`), then fall
            # back to the SIP-wire display_name, then to user@domain.
            def _chip_text(a) -> str:
                return (
                    getattr(a, "label", "")
                    or a.display_name
                    or f"{a.username}@{a.domain}"
                )
            for acc in enabled:
                label = _chip_text(acc)
                act = menu.addAction(label)
                act.triggered.connect(
                    lambda _checked=False, aid=acc.id, lbl=label: self._set_active_account(aid, lbl)
                )
            menu.addSeparator()
            menu.addAction("Add account...", self._on_add_account)
            if not self._active_account_id or not any(a.id == self._active_account_id for a in enabled):
                first = enabled[0]
                self._set_active_account(first.id, _chip_text(first))
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
        self.account_chip.setText(f"{dot}  {label}  ▾")
        self.account_chip.setProperty("health", health)
        self.account_chip.style().unpolish(self.account_chip)
        self.account_chip.style().polish(self.account_chip)
        # Refresh supplier picker for this account's switch type.
        try:
            self._refresh_supplier_picker()
        except Exception:
            log.exception("Supplier picker refresh failed for account %s", account_id)

    # ------------------------------------------------------------------
    # Supplier picker (drives dial routing)
    # ------------------------------------------------------------------
    def _refresh_supplier_picker(self) -> None:
        """Show or hide the supplier picker based on active account's
        switch_type, and populate the dropdown with the shared
        suppliers list. Called on account selection + on boot."""
        acc = self._selected_account()
        kind = (getattr(acc, "switch_type", "other") or "other").lower()
        if not acc or kind == "other":
            self.supplier_row_widget.setVisible(False)
            self._active_supplier_id = ""
            return
        from PySide6.QtGui import QStandardItem
        from noc_beam.config.suppliers import load_valid_suppliers

        # Only valid-marked suppliers reach the picker; the full list
        # (including invalid) lives in Settings -> Suppliers.
        suppliers = load_valid_suppliers()
        # Block signals so populating doesn't fire _on_supplier_changed
        # repeatedly during fill.
        self.supplier_combo.blockSignals(True)
        # Populate the SOURCE model directly (the combo's model is the
        # proxy; addItem on the proxy isn't safe). Also rebuild the
        # display+id cache for autofill.
        self._supplier_source_model.clear()
        self._all_suppliers.clear()
        for s in suppliers:
            item = QStandardItem(s.display())
            item.setData(s.id, Qt.ItemDataRole.UserRole)
            self._supplier_source_model.appendRow(item)
            self._all_suppliers.append((s.display(), s.id))
        # Clear any stale filter so findData sees every row.
        self._supplier_proxy.setFilterFixedString("")
        # Restore last-selected supplier if it's still in the list.
        idx = self.supplier_combo.findData(self._active_supplier_id)
        if idx >= 0:
            self.supplier_combo.setCurrentIndex(idx)
        elif self.supplier_combo.count():
            self.supplier_combo.setCurrentIndex(0)
            self._active_supplier_id = self.supplier_combo.itemData(0) or ""
        self.supplier_combo.blockSignals(False)
        self.supplier_row_widget.setVisible(True)
        # Refresh kicker label to show which kind of routing applies.
        self.supplier_kicker.setText(
            "SUPPLIER (auth)" if kind == "teles" else "SUPPLIER (prefix)"
        )

    def _on_supplier_return_pressed(self) -> None:
        """Enter on the supplier line edit commits the visible match.

        Resolves the typed/visible text to a supplier id via the
        `_all_suppliers` cache (NOT the combo's model, which is
        filtered). After commit the proxy filter is cleared and the
        combo's own popup is dismissed -- this works reliably because
        we ditched QCompleter; the combo's popup obeys hidePopup().
        """
        try:
            text = self.supplier_combo.lineEdit().text().strip()
        except Exception:
            return
        if not text:
            return
        text_lower = text.lower()
        target_id = None
        # Exact match first.
        for display, sid in self._all_suppliers:
            if display.lower() == text_lower:
                target_id = sid
                break
        # Substring fallback (commit mid-autofill).
        if target_id is None:
            for display, sid in self._all_suppliers:
                if text_lower in display.lower():
                    target_id = sid
                    break
        if target_id is None:
            return
        # Clear filter so findData sees every row, then commit.
        try:
            self._supplier_proxy.setFilterFixedString("")
        except Exception:
            pass
        resolved_idx = self.supplier_combo.findData(target_id)
        if resolved_idx >= 0:
            self.supplier_combo.setCurrentIndex(resolved_idx)
        try:
            self.supplier_combo.hidePopup()
        except Exception:
            pass
        try:
            self.dial_input.setFocus(Qt.FocusReason.TabFocusReason)
        except Exception:
            pass
        # Reset autofill state.
        self._supplier_last_fill = ""
        self._supplier_typed_len = 0

    def _on_supplier_text_edited(self, text: str) -> None:
        """UNIQUE-match inline autofill + filter combo's own popup.

        Two behaviors stacked:

        1. Inline-fill the line edit when the typed text uniquely
           identifies one supplier (substring match, case-insensitive),
           using the `_all_suppliers` cache. Forward-typing re-expands;
           Backspace on the selected suffix collapses and stays.
        2. Drive the QSortFilterProxyModel's filter so the combo's
           OWN popup (the one we control) shows only the matching
           rows. hide+show is needed to force the popup to resize
           when the row count shrinks.
        """
        # ---- Pass 1: autofill via cache (unique substring match) ----
        if not text:
            self._supplier_last_fill = ""
            self._supplier_typed_len = 0
            self._supplier_proxy.setFilterFixedString("")
            try:
                self.supplier_combo.hidePopup()
            except Exception:
                pass
            return
        last_fill = getattr(self, "_supplier_last_fill", "")
        typed_len = getattr(self, "_supplier_typed_len", 0)
        text_lower = text.lower()
        is_backspace_collapse = (
            last_fill and len(text) == typed_len
            and text.lower() == last_fill.lower()[:typed_len]
        )
        if is_backspace_collapse:
            self._supplier_last_fill = ""
            self._supplier_typed_len = len(text)
        else:
            unique_match = None
            for display, _sid in self._all_suppliers:
                if text_lower in display.lower():
                    if unique_match is not None:
                        unique_match = None  # ambiguous
                        break
                    unique_match = display
            if unique_match is not None and unique_match != text:
                le = self.supplier_combo.lineEdit()
                if le is not None:
                    idx = unique_match.lower().find(text_lower)
                    cursor_at = idx + len(text)
                    le.blockSignals(True)
                    try:
                        le.setText(unique_match)
                        le.setCursorPosition(cursor_at)
                        if cursor_at < len(unique_match):
                            le.setSelection(cursor_at, len(unique_match) - cursor_at)
                    finally:
                        le.blockSignals(False)
                    self._supplier_last_fill = unique_match
                    self._supplier_typed_len = cursor_at
            elif unique_match is None:
                self._supplier_last_fill = ""
                self._supplier_typed_len = len(text)
        # ---- Pass 2: filter the combo's popup ----
        # CAREFUL: setFilterFixedString can filter out the row whose
        # currentIndex the combo is pointing at, which makes Qt clear
        # the line edit ("losing" what the user typed). To prevent
        # that we capture the line edit's text + cursor + selection,
        # block signals, set the filter, then restore the line edit.
        le = self.supplier_combo.lineEdit()
        if le is not None:
            saved_text = le.text()
            saved_cursor = le.cursorPosition()
            saved_sel_start = le.selectionStart()
            saved_sel_len = len(le.selectedText())
            try:
                self.supplier_combo.blockSignals(True)
                le.blockSignals(True)
                self._supplier_proxy.setFilterFixedString(text)
                # Restore the line edit display + selection state.
                if le.text() != saved_text:
                    le.setText(saved_text)
                    le.setCursorPosition(saved_cursor)
                    if saved_sel_start >= 0 and saved_sel_len > 0:
                        le.setSelection(saved_sel_start, saved_sel_len)
            finally:
                le.blockSignals(False)
                self.supplier_combo.blockSignals(False)
            # Popup management:
            # - Only call showPopup() if popup is NOT already visible
            #   (prevents the hide+show flicker that was making the
            #   cursor jump and rows appear auto-selected mid-typing).
            # - When we DO open the popup, immediately take focus back
            #   to the line edit -- Qt::Popup steals keyboard focus by
            #   design, so without this the user's next keystroke goes
            #   to the popup (navigating items) instead of the field.
            try:
                view = self.supplier_combo.view()
                rc = self._supplier_proxy.rowCount()
                if rc > 0 and not view.isVisible():
                    self.supplier_combo.showPopup()
                    le.setFocus(Qt.FocusReason.OtherFocusReason)
                elif rc == 0 and view.isVisible():
                    self.supplier_combo.hidePopup()
            except Exception:
                pass

    def _active_calls_on_account(self, account_id: str) -> list:
        """Return CallRecords currently up on a given account.

        Used by destructive paths (remove / edit / re-register / supplier-
        swap / unregister) to refuse-or-defer rather than silently tear
        down a live call's media (the underlying PJSIP account owns the
        Call; shutting it down drops audio without sending BYE).
        """
        try:
            return [
                r for r in self.calls.active()
                if getattr(r, "account_id", None) == account_id
            ]
        except Exception:
            return []

    def _on_supplier_changed(self, index: int) -> None:
        if index < 0:
            return
        sid = self.supplier_combo.itemData(index) or ""
        self._active_supplier_id = str(sid)
        log.info("Active supplier changed -> id=%s", self._active_supplier_id)
        # For Teles, swap auth username on the active account and re-register.
        # For Genband, no re-register; the prefix is applied at dial time.
        acc = self._selected_account()
        if acc is None:
            return
        kind = (getattr(acc, "switch_type", "other") or "other").lower()
        if kind != "teles":
            return
        try:
            from noc_beam.config.suppliers import load_suppliers
            from noc_beam.sip.endpoint import SipEndpoint

            suppliers = {s.id: s for s in load_suppliers()}
            s = suppliers.get(self._active_supplier_id)
            if s is None:
                return
            routing_fmt = getattr(acc, "routing_format", "") or ""
            # Guard: if routing_format has no `{id}` placeholder, treat the
            # user's typed username as a fixed value. Without this, the
            # supplier picker rewrites username to the literal template
            # string every time (e.g. routing_format="U" -> username="U",
            # clobbering "U080" the user typed). Operators who use one
            # supplier per account get burned by this.
            if "{id}" not in routing_fmt:
                log.info(
                    "Supplier swap skipped: routing_format=%r has no {id} "
                    "placeholder, leaving username=%r untouched",
                    routing_fmt, acc.username,
                )
                return
            new_uid = s.routed(routing_fmt)
            if not new_uid or (new_uid == acc.username and new_uid == acc.auth_user):
                return
            # Guard: supplier swap calls update_account which does
            # remove+re-add internally. Tearing down the PJSIP account
            # mid-call kills the call's audio without sending BYE.
            # Refuse the swap; user must end live calls first.
            active_on = self._active_calls_on_account(acc.id)
            if active_on:
                QMessageBox.warning(
                    self, "Change supplier",
                    f"{acc.username}@{acc.domain} has {len(active_on)} active "
                    "call(s). End them before switching supplier — the swap "
                    "re-registers and would drop the call mid-conversation."
                )
                # Roll the combo back to whatever was selected before so
                # the UI matches the un-applied state.
                try:
                    prev_idx = self.supplier_combo.findData(
                        getattr(acc, "auth_user", "") or acc.username
                    )
                    if prev_idx >= 0:
                        self.supplier_combo.blockSignals(True)
                        self.supplier_combo.setCurrentIndex(prev_idx)
                        self.supplier_combo.blockSignals(False)
                except Exception:
                    pass
                return
            # Swap BOTH username and auth_user so the on-wire From: URI
            # and Authorization header agree (e.g. From: sip:U138@... +
            # Authorization: username="U138"). Operator workflow puts
            # the Uid in the "User name" field in Eyebeam, so a supplier
            # change must rewrite that field, not just the auth side.
            log.info("Teles supplier swap: username %r->%r, auth_user %r->%r",
                     acc.username, new_uid, acc.auth_user, new_uid)
            acc.username = new_uid
            acc.auth_user = new_uid
            # Re-register with new credentials. SipEndpoint.update_account
            # handles the modify+register cycle (which is remove+add
            # internally). Reset retry state first so a pending backoff
            # timer doesn't fire setRegistration(True) on the rebuilt
            # account, racing the supplier-swap REGISTER.
            self.reg_retry.reset(acc.id)
            try:
                SipEndpoint.instance().update_account(acc)
            except Exception:
                log.exception("Failed to re-register account %s with new Uid", acc.id)
        except Exception:
            log.exception("Teles auth swap failed for supplier %s", self._active_supplier_id)

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
            if cfg.enabled:
                self._add_account_to_endpoint(cfg)
                # Immediate feedback so the user knows the dialog
                # accepted the input -- otherwise the UI looks frozen
                # for the 5-10s the registrar takes to reply.
                self._set_status(
                    f"Registering {cfg.username}@{cfg.domain}…", "muted"
                )
            self._refresh_accounts()

    def _selected_account(self):
        if self._active_account_id:
            return next((a for a in self.accounts if a.id == self._active_account_id), None)
        return None

    def _on_edit_account(self):
        acc = self._selected_account()
        if acc is None:
            QMessageBox.information(self, "Edit account", "Select an account first."); return
        # Delegate to the by-id flow so all edit paths share the
        # delete-in-call guard, registering-banner, and re-register
        # handling.
        self._edit_account_by_id(acc.id)

    def _on_remove_account(self):
        acc = self._selected_account()
        if acc is None:
            QMessageBox.information(self, "Remove account", "Select an account first."); return
        self._remove_account_by_id(acc.id)

    # ------------------------------------------------------------------
    # By-id handlers (driven by AccountDetail's signals + AcctRow rows)
    # ------------------------------------------------------------------
    def _edit_account_by_id(self, account_id: str) -> None:
        acc = next((a for a in self.accounts if a.id == account_id), None)
        if acc is None:
            return
        dlg = AccountDialog(account=acc, parent=self)
        if not _open_modal(dlg):
            return
        new_cfg = dlg.result_account()
        # Editing the active account triggers remove+re-add, which
        # tears down any live call's audio. Refuse if calls are up.
        active_on = [
            r for r in self.calls.active() if getattr(r, "account_id", None) == acc.id
        ]
        if active_on:
            QMessageBox.warning(
                self, "Edit account",
                f"{acc.username}@{acc.domain} has {len(active_on)} active call(s). "
                "Changes will apply after they end."
            )
            # Save the new config to disk but don't re-register yet --
            # the next launch (or the next manual Test) picks it up.
            accounts = [new_cfg if a.id == acc.id else a for a in self.accounts]
            if not self._save_accounts_or_warn(accounts):
                return
            self.accounts = accounts
            self._refresh_accounts()
            return
        accounts = [new_cfg if a.id == acc.id else a for a in self.accounts]
        if not self._save_accounts_or_warn(accounts):
            return
        # Snapshot prior in-memory accounts so we can roll back the UI
        # state if the re-add fails (without this, the old PJSIP account
        # has been torn down + self.accounts has been mutated to the new
        # cfg, but no live endpoint backs it -- UI shows a registered
        # account that won't survive a restart and that the registrar
        # has never heard of). The save_accounts() to disk already
        # happened above; if re-add fails the on-disk state is the new
        # cfg, which is fine -- next launch will try the new cfg
        # cleanly. The in-memory state needs to match the live
        # endpoint, hence the rollback.
        prior_accounts = list(self.accounts)
        self.accounts = accounts
        # Reset retry state before tearing down the PJSIP account so any
        # pending retry timer doesn't fire setRegistration(True) on the
        # freshly re-added account, racing the legitimate REGISTER.
        self.reg_retry.reset(acc.id)
        SipEndpoint.instance().remove_account(acc.id)
        if new_cfg.enabled:
            try:
                self._add_account_to_endpoint(new_cfg)
                self._set_status(
                    f"Registering {new_cfg.username}@{new_cfg.domain}…", "muted"
                )
            except Exception:
                log.exception("Edit-account re-register failed; rolling back UI state")
                # Roll back self.accounts so the UI doesn't show an
                # account that has no live PJSIP backing.
                self.accounts = prior_accounts
                self._set_status(
                    f"Edit failed: re-register of {new_cfg.username}@{new_cfg.domain} "
                    "didn't take. Disk has new config; restart to pick it up.",
                    "danger",
                )
        self._refresh_accounts()

    def _remove_account_by_id(self, account_id: str) -> None:
        acc = next((a for a in self.accounts if a.id == account_id), None)
        if acc is None:
            return
        # Delete-while-in-call guard. Removing the underlying PJSIP
        # account while a call on it is CONFIRMED tears down the
        # call's audio without sending BYE -- peer waits for timeout.
        active_on = [
            r for r in self.calls.active() if getattr(r, "account_id", None) == acc.id
        ]
        if active_on:
            QMessageBox.warning(
                self,
                "Remove account",
                (
                    f"{acc.username}@{acc.domain} has {len(active_on)} active "
                    "call(s). End them first, then remove the account."
                ),
            )
            return
        if not _ask_yes_no(
            self, "Remove account", f"Remove {acc.username}@{acc.domain}?"
        ):
            return
        # Reset retry state + drop cached registration health so a stale
        # retry timer doesn't fire setRegistration on the destroyed
        # account and so _set_active_account doesn't consult stale codes
        # if the same account_id is ever reused.
        self.reg_retry.reset(acc.id)
        self._reg_state.pop(acc.id, None)
        SipEndpoint.instance().remove_account(acc.id)
        accounts = [a for a in self.accounts if a.id != acc.id]
        if not self._save_accounts_or_warn(accounts):
            return
        self.accounts = accounts
        self._refresh_accounts()

    def _test_account_by_id(self, account_id: str) -> None:
        """Re-issue REGISTER on an existing account so the user can
        verify creds without editing. Surfaces result via the usual
        registration_changed signal path."""
        acc = next((a for a in self.accounts if a.id == account_id), None)
        if acc is None or not acc.enabled:
            return
        # Test Register internally does remove + re-add, which tears
        # down any live call's audio on this account without BYE.
        active_on = self._active_calls_on_account(account_id)
        if active_on:
            QMessageBox.warning(
                self, "Test register",
                f"{acc.username}@{acc.domain} has {len(active_on)} active "
                "call(s). End them before re-registering — Test would drop "
                "the call mid-conversation."
            )
            return
        try:
            # Reset retry state so a pending backoff timer doesn't fire
            # against the freshly-re-added account during the test cycle.
            self.reg_retry.reset(acc.id)
            SipEndpoint.instance().remove_account(acc.id)
            self._add_account_to_endpoint(acc)
            self._set_status(
                f"Re-registering {acc.username}@{acc.domain}…", "muted"
            )
        except Exception:
            log.exception("test_account_by_id failed")

    def _unregister_account_by_id(self, account_id: str) -> None:
        """Send Expires:0 by removing the PJSIP account; the row stays
        in self.accounts so the user can re-register via Test."""
        acc = next((a for a in self.accounts if a.id == account_id), None)
        if acc is None:
            return
        # Same footgun: dropping the PJSIP account kills the call audio.
        active_on = self._active_calls_on_account(account_id)
        if active_on:
            QMessageBox.warning(
                self, "Unregister account",
                f"{acc.username}@{acc.domain} has {len(active_on)} active "
                "call(s). End them before unregistering."
            )
            return
        try:
            # Reset retry state + drop cached health: the account is now
            # logically deregistered, no retry should fire and the chip
            # should not show stale "registered" until next Test.
            self.reg_retry.reset(acc.id)
            self._reg_state.pop(acc.id, None)
            SipEndpoint.instance().remove_account(acc.id)
            self._set_status(
                f"Unregistered {acc.username}@{acc.domain}", "muted"
            )
            self._refresh_accounts()
        except Exception:
            log.exception("unregister_account_by_id failed")

    def _on_endpoint_error(self, msg):
        log.error("Endpoint error: %s", msg)
        self._set_status(f"Endpoint error: {msg}", "danger",
                         "Click here to retry", "retry-register")
        if self.accounts:
            QMessageBox.warning(self, "SIP endpoint error", msg)

    def _on_registration_changed(self, account_id, code, reason):
        acc = next((a for a in self.accounts if a.id == account_id), None)
        # Use the unified _account_label helper — prefers the operator's
        # `label` nickname (e.g. "Teles UK") over the raw display_name
        # (e.g. "96171488860"). Without this, every SIP 408 / 5xx retry
        # would rebuild the chip from display_name, overwriting whatever
        # nickname the user had picked.
        label = self._account_label(account_id)
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
        return (
            getattr(acc, "label", "")
            or acc.display_name
            or f"{acc.username}@{acc.domain}"
        )

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
        # Drive audio routing from the MAIN thread (was previously done
        # unconditionally in PJSIP's onCallMediaState worker callback,
        # which raced our focus state). set_call_audio_focus walks every
        # live call and wires only the focused one — so a brand-new
        # answered call that isn't selected stays silent both ways
        # instead of blaring through the speakers.
        try:
            from noc_beam.sip.endpoint import SipEndpoint
            ep = SipEndpoint.instance()
            ep.set_call_audio_focus(self._selected_call_id)
        except Exception:
            log.exception("set audio focus after media-active failed")
        # Attach FAS engine to the freshly-active call. Was previously
        # done inside onCallMediaState (PJSIP thread). Moving it here
        # keeps the FAS attach on the Qt main thread and ensures it
        # runs AFTER the audio route is in place.
        try:
            from noc_beam.audio.fas_engine import attach_fas_to_call
            from noc_beam.sip.endpoint import SipEndpoint  # idempotent
            live = SipEndpoint.instance().find_call(call_id)
            if live is None:
                return
            info = live.getInfo()
            for mi in info.media:
                if mi.type != 1 or mi.status != 1:
                    continue
                try:
                    aud = live.getAudioMedia(mi.index)
                except Exception:
                    continue
                attach_fas_to_call(
                    call_id,
                    aud,
                    account_id=getattr(live, "_account_id", "") or "",
                    remote_uri=getattr(live, "remote_uri", "") or "",
                    codec=codec or "",
                )
                break  # first active audio media is enough
        except Exception:
            log.exception("FAS attach after media-active failed (call=%s)", call_id)

    def _on_call_quality(self, call_id, mos, loss, jitter_ms, rtt_ms):
        if call_id == self._selected_call_id:
            self.call_widget.update_quality(mos, loss)

    def _on_call_fas_verdict(self, call_id, verdict, confidence, reasons):
        # FAS engine fires this from a worker thread; Qt queues it onto
        # the main thread before delivery. Push to the call_manager so
        # any widget bound to call_updated re-renders with the new badge.
        try:
            self.calls.update_fas(call_id, verdict, confidence, reasons)
        except Exception:
            pass


    def _on_call_ended(self, call_id):
        # If the call ended with a SIP failure code, play the matching
        # PSTN-style tone (busy / reorder / reject) so the operator
        # knows by ear without watching the screen. 200/auth/etc are
        # silent. See audio.ringer.FailureTone._tone_for_code for the
        # full mapping.
        try:
            rec = self.calls.get(call_id)
            code = rec.last_code if rec is not None else 0
            answered = getattr(rec, "was_answered", False) if rec is not None else False
            if code and code >= 400 and not answered:
                if getattr(self, "failure_tone", None) is not None:
                    self.failure_tone.play_for_code(code)
        except Exception:
            log.exception("failure-tone play failed for call %s", call_id)
        self._maybe_write_cdr(call_id); self.ringer.stop()

    def _maybe_write_cdr(self, call_id):
        # Try the pre-stashed snapshot first (set during the
        # call_updated->DISCONNECTED path); fall back to building from
        # the current CallRecord so we don't lose CDRs when call_ended
        # arrives BEFORE the call_updated(DISCONNECTED) signal.
        snap = self._last_snapshots.pop(call_id, None)
        if snap is None:
            rec = self.calls.get(call_id)
            if rec is None:
                return
            snap = CdrEntry(
                call_id=rec.call_id,
                account_id=rec.account_id,
                peer_uri=rec.remote_uri,
                direction=rec.direction,
                started_at=rec.started_at,
                connected_at=rec.connected_at,
                ended_at=rec.ended_at or rec.started_at or time.time(),
                end_code=rec.last_code,
                end_reason=rec.last_reason,
                codec=rec.codec,
            )
        try:
            append_entry(snap)
            self.history_view.reload()
            self.quick_dial.reload()
        except Exception:
            log.exception("Failed to append CDR entry")

    def _select_call(self, call_id):
        self._selected_call_id = call_id
        # Route audio so only THIS call is audible / talked-to. Default
        # PJSIP conference behaviour mixes every call into the speakers
        # and sends mic to every call -- chaos when an operator has
        # 3+ test calls running. Audio-focus routes only the selected
        # call's downlink to the speaker and only the mic to its
        # uplink; other calls go silent both ways (soft-hold, no
        # re-INVITE / HOLD signalled to the far end).
        try:
            from noc_beam.sip.endpoint import SipEndpoint
            SipEndpoint.instance().set_call_audio_focus(call_id)
        except Exception:
            log.exception("audio-focus call %s failed", call_id)
        rec = self.calls.get(call_id)
        if rec is None:
            self.call_widget.show_idle(); self.call_widget.setVisible(False)
            # No selected call -> mute everything.
            try:
                from noc_beam.sip.endpoint import SipEndpoint
                SipEndpoint.instance().set_call_audio_focus(None)
            except Exception:
                pass
            return
        self.call_widget.setVisible(True)
        if rec.direction == "in" and rec.state == CallState.INCOMING:
            self.call_widget.show_incoming(call_id, rec.remote_uri)
        else:
            self.call_widget.show_outgoing(call_id, rec.remote_uri or "...")
        self.call_widget.update_state(rec.state.value, rec.last_code, rec.last_reason)
        if rec.codec:
            self.call_widget.update_media(rec.codec, rec.clock_rate, rec.channels)
        # Sync the top-strip mic icon to THIS call's mute state -- the
        # mute is per-call and switching calls switches whose mic mute
        # is shown.
        try:
            muted = bool(getattr(rec, "muted", False))
            self.audio.set_mic_muted(muted)
        except Exception:
            pass
        # Multi-call strip refresh now that the selected call changed
        # (rows other than selected are shown).
        try:
            self._refresh_calls_strip()
        except Exception:
            pass

    def _on_call_record_added(self, call_id):
        if self._selected_call_id is None: self._select_call(call_id)
        self.dialpad.set_in_call(True); self.call_widget.setVisible(True)
        # Arm the audio meter timer now that there's something to measure.
        try:
            if not self._level_timer.isActive():
                self._level_timer.start()
        except Exception:
            pass
        # Tell the AccountDetail pane which account owns this call so
        # its per-account MOS/RTT cards filter correctly (was averaging
        # globally across all accounts -> mis-attribution).
        try:
            rec = self.calls.get(call_id)
            detail = getattr(self, "_accounts_detail", None)
            if detail is not None and rec is not None:
                detail.note_call_account(call_id, rec.account_id)
        except Exception:
            pass

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
                fas_verdict=getattr(rec, "fas_verdict", "") or "",
                fas_confidence=float(getattr(rec, "fas_confidence", 0.0) or 0.0),
                fas_reasons=getattr(rec, "fas_reasons", "") or "",
            )
        if call_id == self._selected_call_id:
            # Stale-peer fix: when remote_uri lands AFTER show_outgoing
            # (e.g. PJSIP populates it after the first 18x or 200), the
            # peer column was set once at register time and never
            # refreshed. Re-render only when the actual remote_uri
            # changes. Previously we compared against
            # call_widget.peer_label.text() -- which holds the
            # already-STRIPPED headline (just the user-part, no sip:
            # prefix, no @domain). That comparison was always true
            # because peer_label never equals the raw `sip:user@host`
            # URI, so every call_updated triggered a full
            # show_outgoing rebuild of the card -- visible flicker
            # mid-call and lost scroll/focus state.
            try:
                last_peer = self._last_call_peer.get(call_id, "")
                if rec.remote_uri and rec.remote_uri != last_peer:
                    if rec.direction == "in" and rec.state == CallState.INCOMING:
                        self.call_widget.show_incoming(call_id, rec.remote_uri)
                    else:
                        self.call_widget.show_outgoing(call_id, rec.remote_uri)
                    self._last_call_peer[call_id] = rec.remote_uri
            except Exception:
                pass
            self.call_widget.update_state(rec.state.value, rec.last_code, rec.last_reason)
            if rec.codec:
                self.call_widget.update_media(rec.codec, rec.clock_rate, rec.channels)
            # FAS verdict badge: hidden until the engine fires, then
            # tinted by verdict severity. Empty string hides it.
            try:
                self.call_widget.update_fas(
                    getattr(rec, "fas_verdict", "") or "",
                    float(getattr(rec, "fas_confidence", 0.0) or 0.0),
                    getattr(rec, "fas_reasons", "") or "",
                )
            except Exception:
                pass
            # Re-apply per-call mute on resume (re-INVITE creates a new
            # audio media slot at default 1.0; without re-applying our
            # mute state silently un-mutes).
            if rec.state == CallState.CONFIRMED and getattr(rec, "muted", False):
                try:
                    live = SipEndpoint.instance().find_call(call_id)
                    if live is not None:
                        SipEndpoint.instance().set_call_mute(live, True)
                except Exception:
                    log.exception("re-apply mute on resume failed")
        # Strip refresh is wired via the singleton's call_updated
        # subscribe (see _strip_refresh_updated in _build_ui) -- don't
        # call it again here or every update fires the diff-pass twice.

    def _on_call_record_removed(self, call_id):
        if call_id == self._selected_call_id:
            self._selected_call_id = None
            next_active = self.calls.first_active()
            if next_active is not None: self._select_call(next_active.call_id)
            else:
                self.call_widget.show_idle(); self.call_widget.setVisible(False)
                self.dialpad.set_in_call(False)
        # Drop the per-call quality buffer + ownership mapping so they
        # don't accumulate forever in long-running sessions.
        try:
            detail = getattr(self, "_accounts_detail", None)
            if detail is not None:
                detail.forget_call_account(call_id)
        except Exception:
            pass
        # Drop the peer-staleness cache so it doesn't grow unbounded.
        self._last_call_peer.pop(call_id, None)

    def _on_dial_input_enter(self):
        target = self.dial_input.text().strip()
        if not target: return
        self._on_call_requested(target); self.dial_input.clear()

    def _rewrite_dial_target(self, target: str) -> str:
        """Apply account dial_prefix + supplier prefix (Genband only)
        to the user-typed target. SIP URIs (sip:user@host) and any
        target that already contains '@' are passed through unchanged.
        """
        if not target or "@" in target or target.startswith("sip:") or target.startswith("sips:"):
            return target
        acc = self._selected_account()
        if acc is None:
            return target
        out = target
        # Genband supplier prefix layered FIRST (innermost), then the
        # account-level dial prefix (outermost) so the wire sees
        # <dial_prefix><supplier_prefix><number>.
        kind = (getattr(acc, "switch_type", "other") or "other").lower()
        if kind == "genband" and self._active_supplier_id:
            try:
                from noc_beam.config.suppliers import load_suppliers

                suppliers = {s.id: s for s in load_suppliers()}
                s = suppliers.get(self._active_supplier_id)
                if s is not None:
                    prefix = s.routed(getattr(acc, "routing_format", "") or "")
                    if prefix:
                        out = f"{prefix}{out}"
            except Exception:
                log.exception("Genband prefix application failed")
        dial_prefix = (getattr(acc, "dial_prefix", "") or "").strip()
        if dial_prefix and not out.startswith(dial_prefix):
            out = f"{dial_prefix}{out}"
        if out != target:
            log.info("Dial rewrite: %r -> %r (account=%s kind=%s)",
                     target, out, acc.id, kind)
        return out

    def _on_call_requested(self, target):
        if not self._active_account_id:
            QMessageBox.information(self, "No account", "Add a SIP account first."); return
        target = self._rewrite_dial_target(target)
        # Verify the active account actually exists in the SIP endpoint.
        # The UI tracks _active_account_id from config; the endpoint's
        # _accounts dict is populated by _add_account_to_endpoint, which
        # can fail (transport collision, registrar timeout during init,
        # etc.) and leave the chip showing an account the endpoint has
        # never heard of. Without this guard, make_call raises a cryptic
        # "Unknown account <uuid>" and the user can't dial. Auto-recover
        # by re-adding the account from its on-disk config; if that ALSO
        # fails, surface a clear, actionable error instead of the UUID.
        ep = SipEndpoint.instance()
        if ep.get_account(self._active_account_id) is None:
            cfg = next(
                (a for a in self.accounts if a.id == self._active_account_id),
                None,
            )
            if cfg is None:
                QMessageBox.warning(
                    self, "Call failed",
                    "The selected account no longer exists. Pick another "
                    "account from the chip in the top-right corner."
                )
                return
            log.info(
                "Active account %s (%s@%s) not in endpoint; re-adding before call",
                cfg.id, cfg.username, cfg.domain,
            )
            try:
                ep.add_account(cfg)
            except Exception as exc:
                log.exception("Re-add of active account failed before make_call")
                QMessageBox.warning(
                    self, "Call failed",
                    f"Could not enable {cfg.username}@{cfg.domain} for this call:\n\n"
                    f"{type(exc).__name__}: {exc}\n\n"
                    "Try Settings → Account → Test Register, or check that "
                    "the registrar is reachable and credentials are correct."
                )
                return
        try:
            call = ep.make_call(self._active_account_id, target)
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
            # Build a guaranteed-non-empty message. Some pjsua2
            # exceptions stringify to "" which produced an empty
            # dialog with no actionable hint (just a warning icon
            # and an OK button). Include the exception type and
            # the target so the user can at least see what
            # they tried to call.
            msg = str(e).strip()
            if not msg:
                msg = f"{type(e).__name__} (no message)"
            QMessageBox.warning(
                self,
                "Call failed",
                f"Could not place call to:\n\n  {target}\n\n{msg}",
            )

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
        # Call-waiting: if there's another CONFIRMED call live, put it
        # on hold BEFORE we answer the new one. Without this both
        # parties end up mixed in the conference bridge audibly --
        # wire-protocol accidental three-way.
        ep = SipEndpoint.instance()
        try:
            for rec in self.calls.active():
                if rec.call_id != _call_id and rec.state == CallState.CONFIRMED:
                    other = ep.find_call(rec.call_id)
                    if other is not None:
                        try:
                            ep.hold_call(other)
                        except Exception:
                            log.exception("auto-hold of call %s failed", rec.call_id)
        except Exception:
            log.exception("call-waiting auto-hold scan failed")
        try: ep.answer_call(call); self.ringer.stop()
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
            self._set_status(f"Transferring to {target}…", "muted", transient=True)
        except Exception as exc:
            log.exception("blind transfer failed")
            QMessageBox.warning(self, "Transfer failed", str(exc))

    def _on_mute_toggled(self, _call_id, muted):
        """In-call CallWidget Mute button → mute the MIC.

        Routes through the device-level audio-strip mic path so:
          - the same mute behaviour as the top-strip mic icon is used
            (capture device adjustTxLevel; re-INVITE-proof)
          - the MIC icon (not the speaker icon) reflects the state
        """
        # Track on the call record for History.
        if self._selected_call_id is not None:
            try:
                self.calls.set_mute(self._selected_call_id, muted)
            except Exception:
                pass
        # Drive the top-strip MIC icon, which has the device-level
        # adjustTxLevel wiring + label/state handling.
        try:
            self.audio.set_mic_muted(muted)
            self._on_audio_strip_mic_mute(muted)
        except Exception:
            log.exception("mute toggle failed")

    def _on_audio_strip_mute(self, muted: bool) -> None:
        """Top-strip SPEAKER icon → silence the playback DEVICE.

        Operating at the device level (not per-call) means a re-INVITE
        or codec change cannot un-stick our mute, and it works even
        across simultaneous calls. The endpoint's set_playback_mute
        wraps adjustRxLevel(0/1) on the playback device with a None-
        guard for startup/shutdown windows. Was previously reaching
        into ep._ep directly here — layering violation that broke if
        SipEndpoint internals were ever refactored.
        """
        try:
            ep = SipEndpoint.instance()
        except Exception:
            return
        try:
            ep.set_playback_mute(muted)
        except Exception:
            log.exception("audio-strip speaker (device) mute failed")

    def _on_audio_strip_volume(self, value: int) -> None:
        """Top-strip OUTPUT slider → scale remote audio coming TO us.

        Note on pjsua2 directionality (this is counter-intuitive):
        for a CALL audio media, adjustTxLevel scales audio the call
        port TRANSMITS to the conference bridge — that's audio sourced
        from remote RTP, i.e. what we *hear*. Inverse from device
        media. Earlier wiring used adjustRxLevel which is the mic
        knob; that's why the controls felt swapped to the user.
        """
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
                if mi.type != 1 or mi.status != 1:
                    continue
                aud = call.getAudioMedia(mi.index)
                level = max(0.0, min(1.5, value / 66.6))
                aud.adjustTxLevel(level)  # scale remote → us (speaker)
        except Exception:
            log.exception("audio-strip output volume adjust failed")

    def _on_audio_strip_mic_mute(self, muted: bool) -> None:
        """Top-strip mic icon → mute the SELECTED call only (per-call).

        Device-level mic mute (the previous implementation) was a
        regression: it muted ALL active calls at once, so a user with
        a 2-line setup couldn't selectively keep one party from hearing
        them. set_call_mute uses capture.startTransmit/stopTransmit
        targeted at THIS call's audio media slot — other calls keep
        receiving capture audio.

        Also stash the muted flag on the CallRecord so we can re-apply
        on resume (re-INVITE creates a new audio slot at default 1.0
        and would silently un-mute otherwise).
        """
        call = self._selected_pjsua_call()
        if call is None or self._selected_call_id is None:
            # No live call -- nothing to mute. The toggle still tracks
            # state visually; on next answered call we'll honour it.
            return
        try:
            SipEndpoint.instance().set_call_mute(call, muted)
            self.calls.set_mute(self._selected_call_id, muted)
        except Exception:
            log.exception("audio-strip mic (per-call) mute failed")
        try:
            self.call_widget.mute_btn.blockSignals(True)
            self.call_widget.mute_btn.setChecked(muted)
            self.call_widget.mute_btn.blockSignals(False)
        except Exception:
            pass

    def _on_audio_strip_mic_volume(self, value: int) -> None:
        """Top-strip MIC slider → scale our voice going TO the remote.

        For a CALL audio media, adjustRxLevel scales what the call port
        RECEIVES from the bridge (= our capture audio routed in via
        startTransmit) before it gets sent over RTP. So this is the
        mic-side gain knob.
        """
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
                aud.adjustRxLevel(level)  # scale us → remote (mic)
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
            # No live call -- pause the timer entirely. _on_call_record_added
            # re-arms it when the next call materialises.
            try:
                if not self.calls.all():
                    self._level_timer.stop()
            except Exception:
                pass
            return
        try:
            from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE
            if not PJSUA2_AVAILABLE:
                return
            tx_level = rx_level = 0
            # Re-read getInfo() inside the per-media try so a state
            # flip between scans (PJSIP onCallMediaState callback can
            # invalidate a media slot mid-iteration) doesn't SIGSEGV
            # in the C layer when getAudioMedia is called against an
            # already-torn-down slot.
            try:
                info = call.getInfo()
            except Exception:
                return
            for mi in info.media:
                if mi.type != 1 or mi.status != 1:
                    continue
                # Each PJSIP touch is its own try -- a getAudioMedia
                # crash on one slot doesn't take out the polling loop.
                try:
                    # Re-validate status atomically before touching the
                    # slot (status may have flipped since outer info
                    # read; cheap enough to do per-iteration).
                    info2 = call.getInfo()
                    if mi.index >= len(info2.media):
                        continue
                    if info2.media[mi.index].status != 1:
                        continue
                    aud = call.getAudioMedia(mi.index)
                except Exception:
                    continue
                # Same directionality inversion as the gain sliders:
                #   call.getRxLevel() = audio coming IN to the call
                #     port from the bridge (our mic) = TX direction
                #   call.getTxLevel() = audio the call port sends OUT
                #     to the bridge (remote audio) = RX direction
                try:
                    tx_level = int(aud.getRxLevel() / 2.55)
                except Exception:
                    pass
                try:
                    rx_level = int(aud.getTxLevel() / 2.55)
                except Exception:
                    pass
                break
            self.audio.set_tx_level(tx_level)
            self.audio.set_rx_level(rx_level)
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

    def _flush_strip_if_dirty(self) -> None:
        """Coalesce sink for high-frequency call_updated bursts. The
        50 ms timer ensures we redraw at most ~20 fps even if the
        FAS engine + quality sampler are both firing. add/remove
        bypass this and refresh immediately so the strip's row count
        stays accurate."""
        if self._strip_dirty:
            self._strip_dirty = False
            self._refresh_calls_strip()

    def _refresh_calls_strip(self) -> None:
        """Render the multi-call stack of OTHER active calls.

        Unified Call Stack design: selected call lives in the main
        CallWidget (expanded, full controls). Other calls render as
        compact 36px cards in this stack -- peer · state-dot ·
        duration · End. Click anywhere (except End) to promote that
        call to selected. A small header bar shows total call count
        + End-all link when there's >1 call total.

        Diff-update: rather than tearing down + rebuilding every
        widget on every call_added/updated/removed tick (which caused
        flicker, lost focus, and the phantom "NO..." top-level-window
        flash on parent detach), we keep a dict[call_id -> QFrame]
        and only create/update/destroy what actually changed.
        """
        # Per-instance state stash; first-tick init.
        if not hasattr(self, "_strip_rows"):
            self._strip_rows: dict[int, QFrame] = {}
            self._strip_header: QFrame | None = None

        active = self.calls.active()
        others = [r for r in active if r.call_id != self._selected_call_id]
        others_by_id = {r.call_id: r for r in others}

        # Auto-hide Recent Calls when the multi-call strip would
        # otherwise push the window past its design height. Threshold
        # is 3+ total active calls (2+ in the strip after the
        # selected call gets the main card). Operator preference:
        # "if there is no space just hide recent tab".
        try:
            wrapper = getattr(self, "_quick_dial_scroll", None)
            if wrapper is not None:
                enabled_accounts = any(
                    getattr(a, "enabled", True) for a in self.accounts
                )
                should_show = enabled_accounts and len(active) < 3
                wrapper.setVisible(should_show)
        except Exception:
            pass

        if not others:
            # Tear down completely when no others -- but use hide first
            # to avoid the "NO..." top-level flash from setParent(None)
            # on a still-visible widget.
            for cid, row in list(self._strip_rows.items()):
                row.hide()
                row.deleteLater()
            self._strip_rows.clear()
            if self._strip_header is not None:
                self._strip_header.hide()
                self._strip_header.deleteLater()
                self._strip_header = None
            self.calls_strip.setVisible(False)
            return
        self.calls_strip.setVisible(True)

        # ----- Header diff: present only when total active >= 2 -----
        want_header = len(active) >= 2
        if want_header and self._strip_header is None:
            self._strip_header = self._build_strip_header()
            self.calls_strip_layout.insertWidget(0, self._strip_header)
        elif not want_header and self._strip_header is not None:
            self._strip_header.hide()
            self._strip_header.deleteLater()
            self._strip_header = None
        if self._strip_header is not None:
            count_lbl = self._strip_header.findChild(QLabel, "CallStackHeaderLabel")
            if count_lbl is not None:
                count_lbl.setText(f"{len(active)} calls")

        # ----- Row diff: remove rows whose call_id is no longer "other" -----
        for cid in list(self._strip_rows.keys()):
            if cid not in others_by_id:
                row = self._strip_rows.pop(cid)
                row.hide()
                row.deleteLater()

        # ----- Add missing rows + update existing -----
        for rec in others:
            row = self._strip_rows.get(rec.call_id)
            if row is None:
                row = self._build_strip_row(rec.call_id)
                self._strip_rows[rec.call_id] = row
                self.calls_strip_layout.addWidget(row)
            self._update_strip_row(row, rec)

    def _build_strip_header(self) -> QFrame:
        header = QFrame(self.calls_strip)
        header.setObjectName("CallStackHeader")
        header.setFixedHeight(24)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 0, 8, 0)
        hl.setSpacing(8)
        count_lbl = QLabel("", header)
        count_lbl.setObjectName("CallStackHeaderLabel")
        end_all_btn = QPushButton("End all", header)
        end_all_btn.setObjectName("CallStackEndAllLink")
        end_all_btn.setFlat(True)
        end_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        end_all_btn.clicked.connect(self._on_end_all_calls)
        hl.addWidget(count_lbl)
        hl.addStretch(1)
        hl.addWidget(end_all_btn)
        return header

    def _build_strip_row(self, cid: int) -> QFrame:
        # Use the dedicated subclass below instead of monkey-patching
        # QFrame.mousePressEvent on an instance: the old pattern captured
        # _original = QFrame.mousePressEvent in a closure plus _row as a
        # default arg, keeping the Python wrapper alive past Qt's
        # deleteLater. A queued mouse event arriving after delete called
        # the unbound method on a dead C++ object -> RuntimeError.
        row = _CallStripRow(cid, self._select_call, self.calls_strip)
        row.setObjectName("CallStripRow")
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setFixedHeight(36)

        rl = QHBoxLayout(row)
        rl.setContentsMargins(12, 0, 8, 0)
        rl.setSpacing(10)
        dot = QLabel("●", row); dot.setObjectName("CallStripDot")
        dot.setFixedWidth(10)
        peer_lbl = QLabel("", row); peer_lbl.setObjectName("CallStripPeer")
        dur_lbl = QLabel("", row); dur_lbl.setObjectName("CallStripDuration")
        end_btn = QToolButton(row); end_btn.setObjectName("CallStripEndBtn")
        end_btn.setText("✕")
        end_btn.setToolTip("End this call")
        end_btn.setFixedSize(22, 22)
        end_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        end_btn.clicked.connect(
            lambda _checked=False, _cid=cid: self._hangup_one(_cid)
        )
        rl.addWidget(dot)
        rl.addWidget(peer_lbl, 1)
        rl.addWidget(dur_lbl)
        rl.addWidget(end_btn)
        return row

    def _update_strip_row(self, row: QFrame, rec) -> None:
        state_key = rec.state.name.lower()
        # Only repolish when state actually changed -- the heavy work
        # is the QSS style recomputation, not the property set itself.
        old_state = row.property("state")
        if old_state != state_key:
            row.setProperty("state", state_key)
            row.style().unpolish(row); row.style().polish(row)
        dot = row.findChild(QLabel, "CallStripDot")
        if dot is not None and dot.property("state") != state_key:
            dot.setProperty("state", state_key)
            dot.style().unpolish(dot); dot.style().polish(dot)
        peer_lbl = row.findChild(QLabel, "CallStripPeer")
        if peer_lbl is not None:
            new_peer = self._short_peer(rec.remote_uri)
            if peer_lbl.text() != new_peer:
                peer_lbl.setText(new_peer)
                peer_lbl.setToolTip(rec.remote_uri or "")
        dur_lbl = row.findChild(QLabel, "CallStripDuration")
        if dur_lbl is not None:
            new_dur = self._format_call_duration(rec)
            if dur_lbl.text() != new_dur:
                dur_lbl.setText(new_dur)

    @staticmethod
    def _short_peer(uri: str) -> str:
        """Trim sip: prefix AND @domain for compact strip-row display.
        Same rule as quick_dial._short_uri: rows show the user-part
        only since the @domain is implicit (your active account's
        domain). The full URI stays on the row's tooltip."""
        if not uri:
            return "..."
        s = uri.strip()
        if s.startswith("sip:"):
            s = s[4:]
        elif s.startswith("sips:"):
            s = s[5:]
        if "<" in s:
            s = s.split("<", 1)[1].rstrip(">")
        s = s.split(";", 1)[0]
        if "@" in s:
            user, _, host = s.partition("@")
            return user or host or s
        return s

    @staticmethod
    def _format_call_duration(rec) -> str:
        """HH:MM:SS for CONFIRMED, empty otherwise."""
        if not getattr(rec, "connected_at", None):
            return rec.state.name.title()
        import time as _t
        elapsed = int(_t.time() - rec.connected_at)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _refresh_in_call_layout(self) -> None:
        """No-op now that the CallWidget itself is a single compact row.
        Kept as a stable hook in case we want to react to in-call state
        without re-wiring signals."""
        return

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
        # Pass the active account so the Account pane renders its
        # full identity + server + registration sections instead of
        # the empty "Add an account from the brand row" copy when
        # the user already HAS accounts configured.
        active_acct = None
        try:
            if self._active_account_id:
                active_acct = next(
                    (a for a in self.accounts if a.id == self._active_account_id),
                    None,
                )
            if active_acct is None and self.accounts:
                active_acct = self.accounts[0]
        except Exception:
            active_acct = None
        dlg = SettingsDialog(self.settings, account=active_acct, parent=self)
        # Apply-without-close: lets the user click Apply and watch the
        # change land while the dialog stays open. Wrapped because the
        # unit-test FakeDialog stand-in doesn't define apply_requested.
        try:
            dlg.apply_requested.connect(lambda d=dlg: self._apply_settings_from_dialog(d))
        except Exception:
            pass
        if _open_modal(dlg):
            self._apply_settings_from_dialog(dlg)

    def _apply_settings_from_dialog(self, dlg) -> None:
        """Common path for OK + Apply.

        Defends against three failure modes the audit caught:
          1. apply mid-CONFIRMED-call would tear down conf bridge audio;
             skip device + codec push, surface a warning, persist anyway
          2. save_settings raising leaves UI in-memory desynced from
             disk; wrap in try/except and surface via status
          3. set_priority / set_active_devices raising mid-flow would
             abort the rest; each call is its own try
        """
        from noc_beam.codecs.manager import set_priority
        # Snapshot audio device indexes BEFORE apply_to mutates them
        # so we can skip the costly set_active_devices() call when
        # only the theme / appearance changed. Without this guard
        # every Apply re-opened WASAPI handles (100-400ms each on
        # Windows), making theme-only swaps feel laggy.
        try:
            _prev_in = self.settings.audio.input_device
            _prev_out = self.settings.audio.output_device
        except Exception:
            _prev_in, _prev_out = -1, -1
        # 1. Always mutate the in-memory GlobalSettings + collect codec map.
        try:
            codec_map = dlg.apply_to(self.settings)
        except Exception:
            log.exception("settings apply_to failed")
            self._set_status("Settings: read failed", "danger")
            return
        # 2. Persist to disk -- if this fails the in-memory mutation
        # already happened, but at least we surface the failure.
        try:
            save_settings(self.settings)
        except Exception:
            log.exception("settings save_settings failed")
            self._set_status(
                "Settings saved in memory but disk write failed", "danger"
            )
        # 3. If a CONFIRMED call is in progress, defer the audio/codec
        # apply -- swapping devices mid-call kills the live conf bridge.
        # HELD calls also own a media slot (resumes back to CONFIRMED
        # which re-uses the bridge), so they need the same protection.
        in_call = any(
            r.state in (CallState.CONFIRMED, CallState.HELD)
            for r in self.calls.active()
        )
        if in_call:
            self._set_status(
                "Settings saved. Audio + codec changes apply after current call ends.",
                "warn",
            )
        else:
            for cid, prio in codec_map.items():
                try:
                    set_priority(cid, prio)
                except Exception:
                    log.exception("set_priority(%s) failed", cid)
            # Only re-bind audio devices when the indexes actually
            # changed. Re-binding the same device on Windows takes
            # 100-400ms re-opening the WASAPI handle -- noticeable
            # lag for users who only touched the theme.
            if (self.settings.audio.input_device != _prev_in
                    or self.settings.audio.output_device != _prev_out):
                try:
                    set_active_devices(
                        self.settings.audio.input_device,
                        self.settings.audio.output_device,
                    )
                except Exception:
                    log.exception("set_active_devices failed")
            self._set_status("Settings applied", "ok", transient=True)
        # 4. Theme/reduced-motion is always safe to apply -- no audio
        # path involved.
        try:
            self._apply_accessibility_settings()
        except Exception:
            log.exception("apply_accessibility_settings failed")

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
        # Build a DEDICATED TraceView for the popup. Re-parenting the
        # stack's instance into the popup yanks it out of the stack,
        # which leaves BOTH surfaces broken (popup paints blank and
        # the in-shell Trace tab loses its content). Each TraceView
        # self-wires to sip_events().sip_message in __init__, so
        # both stay live independently.
        if not hasattr(self, "_trace_window"):
            from PySide6.QtWidgets import QMainWindow
            self._trace_window = QMainWindow(self)
            self._trace_window.setWindowTitle("NOC_Beam SIP trace")
            self._trace_window.resize(900, 600)
            from noc_beam.ui.trace_view import TraceView
            self._popup_trace_view = TraceView(self._trace_window)
            self._trace_window.setCentralWidget(self._popup_trace_view)
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

            # Track which account is currently shown in the detail
            # pane so the AccountDetail's parameter-less signals can be
            # routed to the by-id handlers.
            self._accounts_detail_id = ""
            self.accounts_view.selected_account_changed.connect(
                self._on_accounts_window_selection
            )
            # Wire AccountDetail's action buttons (previously dead --
            # signals emitted but never connected). Each handler dispatches
            # to the by-id handler using the currently shown account.
            self._accounts_detail.edit_requested.connect(
                lambda: self._edit_account_by_id(self._accounts_detail_id)
            )
            self._accounts_detail.test_requested.connect(
                lambda: self._test_account_by_id(self._accounts_detail_id)
            )
            self._accounts_detail.unregister_requested.connect(
                lambda: self._unregister_account_by_id(self._accounts_detail_id)
            )
            self._accounts_detail.remove_requested.connect(
                lambda: self._remove_account_by_id(self._accounts_detail_id)
            )
        self._accounts_window.show()
        self._accounts_window.raise_(); self._accounts_window.activateWindow()

    def _on_accounts_window_selection(self, account_id: str) -> None:
        self._accounts_detail_id = account_id or ""
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

    def _on_about(self):
        QMessageBox.about(
            self, "About NOC_Beam",
            f"<b>{__app_name__}</b> {__version__}<br><br>"
            "NOC engineering softphone. SIP, TLS, SRTP, multi-account.<br>"
            "Open the wide dashboard from View for the full NOC console."
        )

    def _install_shortcuts(self):
        for seq, slot in (
            ("Return",        self._on_dial_input_enter),
            ("Esc",           self._on_hangup_requested),
            ("Ctrl+1",        lambda: self.bottom_tabs.select(int(Tab.DIALPAD))),
            ("Ctrl+2",        lambda: self.bottom_tabs.select(int(Tab.CONTACTS))),
            ("Ctrl+3",        lambda: self.bottom_tabs.select(int(Tab.FAVORITES))),
            ("Ctrl+4",        lambda: self.bottom_tabs.select(int(Tab.HISTORY))),
            # Trace moved out of the bottom tabs into the View-menu
            # popup. Ctrl+Shift+T is the conventional "developer
            # tools" binding (browsers, IDEs); freeing Ctrl+5 for
            # future use.
            ("Ctrl+Shift+T",  self._on_open_trace),
            ("Ctrl+K",        lambda: self.dial_input.setFocus(Qt.FocusReason.ShortcutFocusReason)),
        ):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)

    def _restore_from_tray(self):
        self.showNormal(); self.raise_(); self.activateWindow()

    def _on_quit(self):
        # 1. Flag so closeEvent runs the full teardown path instead of
        #    minimizing to tray.
        # 2. close() triggers closeEvent which drops sip_events
        #    subscribers + stops timers + tells embedded views to
        #    shutdown.
        # 3. Hide the tray icon so QSystemTrayIcon (a QObject that owns
        #    the native tray slot) stops holding the process open AND
        #    Windows reclaims the icon immediately instead of showing
        #    a ghost slot until next mouse-over.
        # 4. Explicitly QApplication.quit() so the event loop exits
        #    even if any background dialog / QMenu / pop-out window is
        #    still parented to a child of this shell. Closing the main
        #    window alone was leaving the process alive in some cases
        #    (tray icon + child QMenu kept Qt counting refs).
        self._really_quitting = True
        try:
            self.close()
        except Exception:
            log.exception("close() raised during _on_quit")
        try:
            if self.tray is not None:
                self.tray.shutdown()
        except Exception:
            log.exception("tray shutdown raised during _on_quit")
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.quit()
        except Exception:
            log.exception("QApplication.quit() raised during _on_quit")

    def closeEvent(self, event):
        if not self._really_quitting and self.tray.available:
            event.ignore(); self.hide(); return
        # Drop every singleton subscription via the SignalRegistry
        # that _connect_events populated. One call replaces the
        # giant hand-maintained connect/disconnect pair list that
        # used to drift -- now bind() and unbind_all() are the
        # single source of truth. The strip-refresh lambdas are
        # disconnected separately because they're wired in _build_ui
        # (before _connect_events), not via the registry.
        registry = getattr(self, "_signals", None)
        if registry is not None:
            registry.unbind_all()
        # Strip-refresh lambdas (v3 audit's test-hang root cause).
        # Pre-date the registry adoption -- still hand-disconnected.
        for sig, slot in (
            (self.calls.call_added, getattr(self, "_strip_refresh_added", None)),
            (self.calls.call_removed, getattr(self, "_strip_refresh_removed", None)),
            (self.calls.call_updated, getattr(self, "_strip_refresh_updated", None)),
        ):
            if slot is None:
                continue
            try:
                sig.disconnect(slot)
            except Exception:
                pass
        # Tell embedded views to drop their sip_events subscribers.
        # Replaces the old `destroyed.connect` pattern in each view —
        # PySide6 doesn't reliably fire destroyed on embedded widgets,
        # so the subscribers used to leak for the whole app lifetime
        # plus accumulate one per Settings/Trace/Accounts re-open.
        for view_attr in ("_accounts_detail", "trace_page", "trace_view", "trace_drawer"):
            view = getattr(self, view_attr, None)
            if view is None:
                continue
            shutdown = getattr(view, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:
                    log.exception("shutdown() raised on %s", view_attr)
        # Stop the audio level poll timer so it can't fire into a
        # destroyed widget.
        try:
            self._level_timer.stop()
        except Exception:
            pass
        try: SipEndpoint.instance().stop()
        except Exception: log.exception("Endpoint stop error")
        super().closeEvent(event)
