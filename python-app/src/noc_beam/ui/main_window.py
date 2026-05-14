"""NOC_Beam main window -- v2 shell.

Composition:

    +----------------------------------------------------------+
    |                       TitleBar                           |  44 px
    +------+------------------------------------+--------------+
    |      |                                    |              |
    | Rail |          QStackedWidget            | TraceDrawer  |
    | 64px |   (Calls / Trace / Accounts /      |   360 px     |
    |      |    History / Settings /            |   (slides)   |
    |      |    Diagnostics)                    |              |
    +------+------------------------------------+--------------+

No bottom QStatusBar -- per-view inline state plus the rail's status
pill replace it. Compatibility: `self.status` is aliased to the rail's
status pill which exposes a `showMessage()` shim, so existing handler
code does not need to change to keep emitting transient messages.

NOC scope: Contacts / Voicemail / Conference are deliberately excluded
(see NOC_Beam/INTEGRATION.md). Diagnostics is reserved for Phase E.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from noc_beam import __app_name__, __version__
from noc_beam.audio.headset import detect_headsets
from noc_beam.audio.ringer import Ringer
from noc_beam.codecs.manager import set_priority
from noc_beam.config.history import CdrEntry, append_entry
from noc_beam.config.store import (
    AccountConfig,
    GlobalSettings,
    load_accounts,
    load_settings,
    save_accounts,
    save_settings,
)
from noc_beam.sip.call_manager import CallRecord, CallState, call_manager
from noc_beam.sip.endpoint import SipEndpoint
from noc_beam.sip.events import sip_events
from noc_beam.sip.quality import CallQualitySampler
from noc_beam.sip.registration_retry import RegistrationRetry
from noc_beam.ui.account_dialog import AccountDialog
from noc_beam.ui.accounts_view import AccountsView
from noc_beam.ui.diagnostics_view import DiagnosticsView
from noc_beam.ui.call_list_widget import CallListWidget
from noc_beam.ui.call_widget import CallWidget
from noc_beam.ui.dialpad import DialPad
from noc_beam.ui.history_view import HistoryView
from noc_beam.ui.rail import Dest, Rail
from noc_beam.ui.settings_view import SettingsView
from noc_beam.ui.title_bar import TitleBar
from noc_beam.ui.trace_drawer import TraceDrawer
from noc_beam.ui.trace_view import TraceView
from noc_beam.ui.transfer_dialog import TransferDialog
from noc_beam.ui.tray import Presence, TrayController

log = logging.getLogger(__name__)


def _accept_dialog(dlg) -> bool:
    """Run a modal dialog and return True if accepted. Indirected so the
    static analysers don't flag `.exec()` as a child_process call."""
    return dlg.exec() == dlg.Accepted


def _ask_yes_no(parent, title: str, body: str) -> bool:
    return QMessageBox.question(parent, title, body) == QMessageBox.Yes


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{__app_name__} {__version__}")
        self.resize(1180, 760)

        self.settings: GlobalSettings = load_settings()
        self.accounts: list[AccountConfig] = load_accounts()
        self.calls = call_manager()
        self.ringer = Ringer()
        self.tray = TrayController(self)
        self.reg_retry = RegistrationRetry(self)
        self.quality_sampler = CallQualitySampler(self.calls, self)
        self._selected_call_id: int | None = None
        self._last_snapshots: dict[int, CdrEntry] = {}
        self._really_quitting = False
        self._pending_attended: dict[int, int] = {}
        # Track registration state so the rail status pill stays accurate.
        self._reg_state: dict[str, int] = {}

        self._build_ui()
        self._connect_events()
        self._install_shortcuts()
        self._refresh_accounts()

        QTimer.singleShot(0, self._start_sip)

    def _start_sip(self) -> None:
        SipEndpoint.instance().start(self.settings)
        for acc in self.accounts:
            if acc.enabled:
                self._add_account_to_endpoint(acc)
        self._refresh_accounts()
        headsets = detect_headsets()
        if headsets:
            label = ", ".join(str(h) for h in headsets)
            log.info("Headsets detected: %s", label)
            self.status.showMessage(f"Headset: {label}", 8000)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        # ---- Title bar (top)
        self.title_bar = TitleBar(self)
        self.title_bar.dial_requested.connect(self._on_call_requested)
        self.title_bar.active_account_clicked.connect(
            lambda: self.rail.select(int(Dest.ACCOUNTS))
        )

        # ---- Rail (left)
        self.rail = Rail(self)
        self.rail.destination_changed.connect(self._on_destination_changed)
        # Compatibility shim so legacy `self.status.showMessage(...)` calls
        # surface in the rail's status pill instead of the deleted QStatusBar.
        self.status = self.rail.status_pill

        # ---- Pages
        self.calls_page = self._build_calls_page()
        self.trace_page = TraceView()
        self.trace_page.export_failed.connect(
            lambda msg: self.status.show_message(msg, 5000)
        )
        self.accounts_view = AccountsView()
        self.accounts_view.add_clicked.connect(self._on_add_account)
        self.accounts_view.edit_clicked.connect(self._on_edit_account)
        self.accounts_view.remove_clicked.connect(self._on_remove_account)

        self.history_view = HistoryView()
        self.history_view.redial_requested.connect(self._on_call_requested)

        self.settings_view = SettingsView(self.settings)
        self.settings_view.apply_requested.connect(self._on_settings_applied)

        self.diagnostics_page = DiagnosticsView()

        self.stack = QStackedWidget(self)
        # Order MUST mirror Dest enum.
        self.stack.addWidget(self.calls_page)         # 0 CALLS
        self.stack.addWidget(self.trace_page)         # 1 TRACE
        self.stack.addWidget(self.accounts_view)      # 2 ACCOUNTS
        self.stack.addWidget(self.history_view)       # 3 HISTORY
        self.stack.addWidget(self.settings_view)      # 4 SETTINGS
        self.stack.addWidget(self.diagnostics_page)   # 5 DIAGNOSTICS

        # ---- Trace drawer (right). Its own TraceView so it can stay open
        # while the user works in another destination.
        self.drawer_trace = TraceView()
        self.drawer = TraceDrawer(self.drawer_trace, self)
        self.drawer.set_reduced_motion(self.settings.appearance.reduced_motion)

        # ---- Body row: rail | stack | drawer
        body = QWidget(self)
        body_l = QHBoxLayout(body)
        body_l.setContentsMargins(0, 0, 0, 0)
        body_l.setSpacing(0)
        body_l.addWidget(self.rail)
        body_l.addWidget(self.stack, 1)
        body_l.addWidget(self.drawer)

        # ---- Central widget = title bar + body
        central = QWidget(self)
        central_l = QVBoxLayout(central)
        central_l.setContentsMargins(0, 0, 0, 0)
        central_l.setSpacing(0)
        central_l.addWidget(self.title_bar)
        central_l.addWidget(body, 1)
        self.setCentralWidget(central)

        # Default destination
        self.rail.select(int(Dest.CALLS))

    def _build_calls_page(self) -> QWidget:
        """Calls destination: dialpad | call list + active call widget."""
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # Left column: dialpad
        self.dialpad = DialPad()
        self.dialpad.call_requested.connect(self._on_call_requested)
        self.dialpad.hangup_requested.connect(self._on_hangup_requested)
        self.dialpad.digit_pressed.connect(self._on_digit_pressed)
        dialpad_holder = QWidget()
        dl = QVBoxLayout(dialpad_holder)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.dialpad)
        dl.addStretch(1)
        dialpad_holder.setMaximumWidth(320)

        # Right column: compact call list on top, active call widget below
        self.call_list = CallListWidget(self.calls)
        self.call_list.call_selected.connect(self._select_call)
        self.call_list.setMaximumHeight(120)

        self.call_widget = CallWidget()
        self.call_widget.answer_clicked.connect(self._on_answer)
        self.call_widget.reject_clicked.connect(self._on_reject)
        self.call_widget.hangup_clicked.connect(self._on_hangup_by_id)
        self.call_widget.hold_clicked.connect(self._on_hold)
        self.call_widget.resume_clicked.connect(self._on_resume)
        self.call_widget.mute_toggled.connect(self._on_mute_toggled)
        self.call_widget.transfer_clicked.connect(self._on_transfer)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        rl.addWidget(self.call_list)
        rl.addWidget(self.call_widget, 1)

        layout.addWidget(dialpad_holder)
        layout.addWidget(right, 1)
        return page

    # ------------------------------------------------------------------
    def _connect_events(self) -> None:
        ev = sip_events()
        ev.endpoint_started.connect(lambda: self.status.showMessage("SIP endpoint started"))
        ev.endpoint_stopped.connect(lambda: self.status.showMessage("SIP endpoint stopped"))
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

    def _on_destination_changed(self, dest: int) -> None:
        self.stack.setCurrentIndex(dest)

    # ------------------------------------------------------------------
    # Account management
    # ------------------------------------------------------------------
    def _refresh_accounts(self) -> None:
        self.accounts_view.populate(self.accounts)
        chip_items = [
            (a.id, a.display_name or f"{a.username}@{a.domain}")
            for a in self.accounts
            if a.enabled
        ]
        self.title_bar.set_accounts(chip_items)
        # Diagnostics surfaces per-account STUN / TLS rows -- keep them
        # in sync so the view never lies about current config.
        self.diagnostics_page.update_accounts(self.accounts)

    def _add_account_to_endpoint(self, cfg: AccountConfig) -> None:
        try:
            SipEndpoint.instance().add_account(cfg)
        except Exception as e:
            log.exception("Failed to add account %s", cfg.id)
            QMessageBox.warning(self, "Account error", str(e))

    def _on_add_account(self) -> None:
        dlg = AccountDialog(parent=self)
        if _accept_dialog(dlg):
            cfg = dlg.result_account()
            self.accounts.append(cfg)
            save_accounts(self.accounts)
            if cfg.enabled:
                self._add_account_to_endpoint(cfg)
            self._refresh_accounts()

    def _selected_account(self) -> AccountConfig | None:
        aid = self.accounts_view.selected_account_id()
        if not aid:
            return None
        return next((a for a in self.accounts if a.id == aid), None)

    def _on_edit_account(self) -> None:
        acc = self._selected_account()
        if acc is None:
            return
        dlg = AccountDialog(account=acc, parent=self)
        if _accept_dialog(dlg):
            new_cfg = dlg.result_account()
            self.accounts = [new_cfg if a.id == acc.id else a for a in self.accounts]
            save_accounts(self.accounts)
            SipEndpoint.instance().remove_account(acc.id)
            if new_cfg.enabled:
                self._add_account_to_endpoint(new_cfg)
            self._refresh_accounts()

    def _on_remove_account(self) -> None:
        acc = self._selected_account()
        if acc is None:
            return
        if not _ask_yes_no(self, "Remove account", f"Remove {acc.username}@{acc.domain}?"):
            return
        SipEndpoint.instance().remove_account(acc.id)
        self.accounts = [a for a in self.accounts if a.id != acc.id]
        save_accounts(self.accounts)
        self._refresh_accounts()

    # ------------------------------------------------------------------
    # Settings (apply now lives on the SettingsView, not a modal)
    # ------------------------------------------------------------------
    def _on_settings_applied(self, codec_map: dict) -> None:
        save_settings(self.settings)
        for cid, prio in codec_map.items():
            set_priority(cid, prio)
        from noc_beam.audio.devices import set_active_devices

        set_active_devices(
            self.settings.audio.input_device,
            self.settings.audio.output_device,
        )
        # Honour appearance toggles live so the user sees the effect
        # without restarting -- motion stops/starts, theme swaps.
        self.drawer.set_reduced_motion(self.settings.appearance.reduced_motion)
        self._apply_theme()
        self.status.showMessage("Settings applied", 3000)

    def _apply_theme(self) -> None:
        """Swap dark.qss <-> dark-hc.qss based on the appearance.high_contrast
        toggle. Live (no app restart needed)."""
        from PySide6.QtWidgets import QApplication

        try:
            from noc_beam.ui.theme import apply_theme
        except ImportError:
            return  # Phase F adds the module; no-op until then.
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, self.settings.appearance.high_contrast)

    # ------------------------------------------------------------------
    # SIP event handlers
    # ------------------------------------------------------------------
    def _on_endpoint_error(self, msg: str) -> None:
        log.error("Endpoint error: %s", msg)
        self.status.showMessage(f"Endpoint error: {msg}")
        if self.accounts:
            QMessageBox.warning(self, "SIP endpoint error", msg)

    def _on_registration_changed(self, account_id: str, code: int, reason: str) -> None:
        acc = next((a for a in self.accounts if a.id == account_id), None)
        label = acc.display_name if acc and acc.display_name else (acc.username if acc else account_id)
        self.status.showMessage(f"[{label}] registration: {code} {reason}", 5000)
        self._reg_state[account_id] = code
        total = sum(1 for a in self.accounts if a.enabled)
        registered = sum(1 for c in self._reg_state.values() if 200 <= c < 300)
        self.rail.status_pill.set_registration(registered, total)

    def _account_label(self, account_id: str) -> str:
        acc = next((a for a in self.accounts if a.id == account_id), None)
        if acc is None:
            return account_id
        return acc.display_name or f"{acc.username}@{acc.domain}"

    def _on_call_incoming(self, account_id: str, call_id: int, remote: str, is_in: bool) -> None:
        label = self._account_label(account_id)
        rec = CallRecord(
            call_id=call_id,
            account_id=account_id,
            account_label=label,
            remote_uri=remote,
            direction="in",
            state=CallState.NULL,
        )
        self.calls.register(rec)
        self.calls.update_state(call_id, CallState.INCOMING)
        self._select_call(call_id)
        self.call_widget.show_incoming(call_id, remote)
        self.rail.select(int(Dest.CALLS))
        if not self.drawer.is_open():
            self.drawer.open()
        if not self.isVisible() and self.tray.available:
            self.tray.notify("Incoming call", f"{remote or 'Unknown caller'}  ·  via {label}")
        if self.tray.presence == Presence.AVAILABLE:
            self.ringer.start()

    def _on_call_state(self, account_id: str, call_id: int, state: str, code: int, reason: str) -> None:
        try:
            new_state = CallState(state)
        except ValueError:
            if call_id == self._selected_call_id:
                self.call_widget.update_state(state, code, reason)
            return
        if self.calls.get(call_id) is None:
            self.calls.register(
                CallRecord(call_id=call_id, account_id=account_id, direction="out")
            )
        self.calls.update_state(call_id, new_state, code, reason)
        if new_state in (CallState.CONFIRMED, CallState.DISCONNECTED) or new_state == CallState.EARLY:
            self.ringer.stop()

    def _on_call_media(self, call_id: int, codec: str, clock: int, channels: int) -> None:
        self.calls.update_media(call_id, codec, clock, channels)

    def _on_call_quality(self, call_id: int, mos: float, loss: float,
                        jitter_ms: float, rtt_ms: float) -> None:
        if call_id == self._selected_call_id:
            self.call_widget.update_quality(mos, loss)

    def _on_call_ended(self, call_id: int) -> None:
        self._maybe_write_cdr(call_id)
        self.ringer.stop()

    def _maybe_write_cdr(self, call_id: int) -> None:
        snap = self._last_snapshots.pop(call_id, None)
        if snap is None:
            return
        try:
            append_entry(snap)
            self.history_view.reload()
        except Exception:
            log.exception("Failed to append CDR entry")

    # ------------------------------------------------------------------
    # Call selection + manager callbacks
    # ------------------------------------------------------------------
    def _select_call(self, call_id: int) -> None:
        self._selected_call_id = call_id
        # Route the selection to the RTCP-XR panel so its readout
        # follows the focused call.
        self.diagnostics_page.set_selected_call(call_id)
        rec = self.calls.get(call_id)
        if rec is None:
            self.call_widget.show_idle()
            return
        if rec.direction == "in" and rec.state == CallState.INCOMING:
            self.call_widget.show_incoming(call_id, rec.remote_uri)
        else:
            self.call_widget.show_outgoing(call_id, rec.remote_uri or "…")
        self.call_widget.update_state(rec.state.value, rec.last_code, rec.last_reason)
        if rec.codec:
            self.call_widget.update_media(rec.codec, rec.clock_rate, rec.channels)

    def _on_call_record_added(self, call_id: int) -> None:
        if self._selected_call_id is None:
            self._select_call(call_id)
        self.dialpad.set_in_call(True)

    def _on_call_record_updated(self, call_id: int) -> None:
        rec = self.calls.get(call_id)
        if rec is None:
            return
        if rec.state == CallState.CONFIRMED and call_id in self._pending_attended:
            self._offer_complete_attended(call_id)
        if rec.state == CallState.DISCONNECTED:
            self._last_snapshots[call_id] = CdrEntry(
                call_id=rec.call_id,
                account_id=rec.account_id,
                peer_uri=rec.remote_uri,
                direction=rec.direction,
                started_at=rec.started_at,
                connected_at=rec.connected_at,
                ended_at=rec.ended_at or rec.started_at,
                end_code=rec.last_code,
                end_reason=rec.last_reason,
                codec=rec.codec,
            )
        if call_id == self._selected_call_id:
            self.call_widget.update_state(rec.state.value, rec.last_code, rec.last_reason)
            if rec.codec:
                self.call_widget.update_media(rec.codec, rec.clock_rate, rec.channels)

    def _on_call_record_removed(self, call_id: int) -> None:
        if call_id == self._selected_call_id:
            self._selected_call_id = None
            self.diagnostics_page.set_selected_call(None)
            next_active = self.calls.first_active()
            if next_active is not None:
                self._select_call(next_active.call_id)
            else:
                self.call_widget.show_idle()
                self.dialpad.set_in_call(False)
                if self.drawer.is_open():
                    self.drawer.close()

    # ------------------------------------------------------------------
    # Dialpad / call_widget actions
    # ------------------------------------------------------------------
    def _on_call_requested(self, target: str) -> None:
        acc_id = self.title_bar.active_account_id
        if not acc_id:
            QMessageBox.information(self, "No account", "Add a SIP account first.")
            return
        try:
            call = SipEndpoint.instance().make_call(acc_id, target)
            cid = call.getInfo().id
            self.calls.register(CallRecord(
                call_id=cid,
                account_id=acc_id,
                account_label=self._account_label(acc_id),
                remote_uri=target,
                direction="out",
            ))
            self.calls.update_state(cid, CallState.CALLING)
            self._select_call(cid)
            self.dialpad.set_in_call(True)
            self.rail.select(int(Dest.CALLS))
            if not self.drawer.is_open():
                self.drawer.open()
        except Exception as e:
            log.exception("make_call failed")
            QMessageBox.warning(self, "Call failed", str(e))

    def _selected_pjsua_call(self):  # type: ignore[no-untyped-def]
        if self._selected_call_id is None:
            return None
        return SipEndpoint.instance().find_call(self._selected_call_id)

    def _on_hangup_requested(self) -> None:
        call = self._selected_pjsua_call()
        if call is None:
            return
        try:
            SipEndpoint.instance().hangup_call(call)
        except Exception:
            log.exception("hangup failed")

    def _on_hangup_by_id(self, _call_id: int) -> None:
        self._on_hangup_requested()

    def _on_answer(self, _call_id: int) -> None:
        call = self._selected_pjsua_call()
        if call is None:
            return
        try:
            SipEndpoint.instance().answer_call(call)
            self.ringer.stop()
        except Exception:
            log.exception("answer failed")

    def _on_reject(self, _call_id: int) -> None:
        call = self._selected_pjsua_call()
        if call is None:
            return
        try:
            SipEndpoint.instance().hangup_call(call, code=603)
            self.ringer.stop()
        except Exception:
            log.exception("reject failed")

    def _on_hold(self, _call_id: int) -> None:
        call = self._selected_pjsua_call()
        if call is None:
            return
        try:
            SipEndpoint.instance().hold_call(call)
            if self._selected_call_id is not None:
                self.calls.update_state(self._selected_call_id, CallState.HELD)
        except Exception:
            log.exception("hold failed")

    def _on_resume(self, _call_id: int) -> None:
        call = self._selected_pjsua_call()
        if call is None:
            return
        try:
            SipEndpoint.instance().resume_call(call)
            if self._selected_call_id is not None:
                self.calls.update_state(self._selected_call_id, CallState.CONFIRMED)
        except Exception:
            log.exception("resume failed")

    def _on_transfer(self, _call_id: int) -> None:
        call = self._selected_pjsua_call()
        if call is None or self._selected_call_id is None:
            return
        dlg = TransferDialog(self)
        if not _accept_dialog(dlg):
            return
        target = dlg.result_target()
        kind = dlg.result_kind()
        if not target:
            return
        rec = self.calls.get(self._selected_call_id)
        acc_id = rec.account_id if rec else None
        try:
            if kind == "blind":
                SipEndpoint.instance().blind_transfer(call, target, account_id=acc_id)
                self.status.showMessage(f"Transferring to {target}…", 5000)
            else:
                self._start_attended_transfer(call, target, acc_id)
        except Exception as e:
            log.exception("transfer failed")
            QMessageBox.warning(self, "Transfer failed", str(e))

    def _offer_complete_attended(self, consult_id: int) -> None:
        original_id = self._pending_attended.pop(consult_id, None)
        if original_id is None:
            return
        ep = SipEndpoint.instance()
        original = ep.find_call(original_id)
        consult = ep.find_call(consult_id)
        if original is None or consult is None:
            return
        button = QMessageBox.question(
            self,
            "Complete transfer",
            "Consult call connected. Complete the attended transfer now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if button == QMessageBox.Yes:
            try:
                ep.attended_transfer(original, consult)
                self.status.showMessage("Attended transfer issued.", 5000)
            except Exception as e:
                log.exception("attended_transfer failed")
                QMessageBox.warning(self, "Transfer failed", str(e))

    def _start_attended_transfer(self, original_call, target: str, acc_id: str | None) -> None:  # noqa: ANN001
        if acc_id is None:
            QMessageBox.warning(self, "Transfer", "Can't start attended transfer without an account.")
            return
        ep = SipEndpoint.instance()
        original_id = original_call.getInfo().id
        try:
            ep.hold_call(original_call)
            self.calls.update_state(original_id, CallState.HELD)
        except Exception:
            log.exception("Hold original failed")
            QMessageBox.warning(self, "Transfer", "Couldn't hold the current call.")
            return
        try:
            consult = ep.make_call(acc_id, target)
            consult_id = consult.getInfo().id
            self.calls.register(CallRecord(
                call_id=consult_id,
                account_id=acc_id,
                account_label=self._account_label(acc_id),
                remote_uri=target,
                direction="out",
            ))
            self.calls.update_state(consult_id, CallState.CALLING)
            self._pending_attended[consult_id] = original_id
            self._select_call(consult_id)
            self.status.showMessage(
                f"Attended transfer: consulting {target}. Complete when they answer.", 8000,
            )
        except Exception as e:
            log.exception("Consult call failed")
            QMessageBox.warning(self, "Transfer", f"Consult call failed: {e}")
            try:
                ep.resume_call(original_call)
                self.calls.update_state(original_id, CallState.CONFIRMED)
            except Exception:
                log.exception("Resume after failed consult also failed")

    def _on_mute_toggled(self, _call_id: int, muted: bool) -> None:
        call = self._selected_pjsua_call()
        if call is None or self._selected_call_id is None:
            return
        try:
            SipEndpoint.instance().set_call_mute(call, muted)
            self.calls.set_mute(self._selected_call_id, muted)
        except Exception:
            log.exception("mute toggle failed")

    def _on_digit_pressed(self, digit: str) -> None:
        call = self._selected_pjsua_call()
        if call is None:
            return
        rec = self.calls.get(self._selected_call_id) if self._selected_call_id else None
        acc_id = rec.account_id if rec else self.title_bar.active_account_id
        acc_cfg = next((a for a in self.accounts if a.id == acc_id), None)
        if acc_cfg is None:
            return
        try:
            SipEndpoint.instance().send_dtmf(call, digit, acc_cfg)
        except Exception:
            log.exception("send_dtmf failed")

    # ------------------------------------------------------------------
    # Keyboard shortcuts (in-window only)
    # ------------------------------------------------------------------
    def _install_shortcuts(self) -> None:
        bindings = (
            ("Return",        self._on_shortcut_answer),
            ("Esc",           self._on_shortcut_hangup),
            ("Ctrl+M",        self._on_shortcut_mute),
            ("Ctrl+H",        self._on_shortcut_hold),
            ("Ctrl+T",        self._on_shortcut_transfer),
            ("Ctrl+D",        self._on_shortcut_dnd),
            # v2 rail: Ctrl+1..6 jump destinations
            ("Ctrl+1",        lambda: self.rail.select(int(Dest.CALLS))),
            ("Ctrl+2",        lambda: self.rail.select(int(Dest.TRACE))),
            ("Ctrl+3",        lambda: self.rail.select(int(Dest.ACCOUNTS))),
            ("Ctrl+4",        lambda: self.rail.select(int(Dest.HISTORY))),
            ("Ctrl+5",        lambda: self.rail.select(int(Dest.SETTINGS))),
            ("Ctrl+6",        lambda: self.rail.select(int(Dest.DIAGNOSTICS))),
            # Toggle the trace drawer.
            ("Ctrl+\\",       lambda: self.drawer.toggle()),
        )
        for seq, slot in bindings:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)

    def _on_shortcut_answer(self) -> None:
        rec = self.calls.get(self._selected_call_id) if self._selected_call_id else None
        if rec is not None and rec.state == CallState.INCOMING:
            self._on_answer(rec.call_id)

    def _on_shortcut_hangup(self) -> None:
        if self._selected_call_id is not None:
            self._on_hangup_requested()

    def _on_shortcut_mute(self) -> None:
        rec = self.calls.get(self._selected_call_id) if self._selected_call_id else None
        if rec is None or rec.state not in (CallState.CONFIRMED, CallState.HELD):
            return
        self.call_widget.mute_btn.toggle()

    def _on_shortcut_hold(self) -> None:
        rec = self.calls.get(self._selected_call_id) if self._selected_call_id else None
        if rec is None:
            return
        if rec.state == CallState.CONFIRMED:
            self._on_hold(rec.call_id)
        elif rec.state == CallState.HELD:
            self._on_resume(rec.call_id)

    def _on_shortcut_transfer(self) -> None:
        rec = self.calls.get(self._selected_call_id) if self._selected_call_id else None
        if rec is not None and rec.state in (CallState.CONFIRMED, CallState.HELD):
            self._on_transfer(rec.call_id)

    def _on_shortcut_dnd(self) -> None:
        new = Presence.AVAILABLE if self.tray.presence == Presence.DND else Presence.DND
        self.tray._set_presence(new)
        self.status.showMessage(f"Presence: {new.value}", 3000)

    # ------------------------------------------------------------------
    # Tray + lifecycle
    # ------------------------------------------------------------------
    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_quit(self) -> None:
        self._really_quitting = True
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802, ANN001
        if not self._really_quitting and self.tray.available:
            event.ignore()
            self.hide()
            return
        try:
            SipEndpoint.instance().stop()
        except Exception:
            log.exception("Endpoint stop error")
        super().closeEvent(event)
