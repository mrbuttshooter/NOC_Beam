"""QWebChannel bridge object: the single UI<->Python surface for the web
softphone.

UI -> Python: the @Slot methods below. Each routes into the EXISTING
PhoneShell logic host (see the brief's phase-1 strategy: PhoneShell is
instantiated hidden and drives all call orchestration). We deliberately
call PhoneShell's public/`_on_*` handlers rather than re-implementing dial /
hold / mute / DTMF flows -- so the web UI takes exactly the code path the old
Qt dialer used. Phase 2 extracts a headless CallSession service and these
slots bind to it instead.

Python -> UI: WebShell pushes JSON via page.runJavaScript("nb.apply(...)")
(see web_shell.py). This object is push-source-agnostic; it only handles the
inbound direction.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Slot

log = logging.getLogger(__name__)


class WebBridge(QObject):
    """Registered on the QWebChannel as `bridge`. `phone` is the hidden
    PhoneShell logic host; `web` is the visible WebShell (window ops)."""

    def __init__(self, phone, web, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._phone = phone
        self._web = web

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _selected_id(self):
        return getattr(self._phone, "_selected_call_id", None)

    def _selected_record(self):
        cid = self._selected_id()
        if cid is None:
            return None
        try:
            return self._phone.calls.get(cid)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Dialing
    # ------------------------------------------------------------------
    @Slot(str)
    def place_call(self, target: str) -> None:
        target = (target or "").strip()
        if not target:
            return
        try:
            self._phone._on_call_requested(target)
        except Exception:
            log.exception("bridge.place_call failed")

    @Slot(str)
    def redial(self, number: str) -> None:
        self.place_call(number)

    def _record_for(self, call_id: int):
        """Resolve the CallRecord a per-call control targets. Negative /
        missing ids fall back to the selected call (single-call JS paths)."""
        cid = int(call_id)
        if cid < 0:
            cid = self._selected_id()
        if cid is None:
            return None
        try:
            return self._phone.calls.get(cid)
        except Exception:
            return None

    @Slot(int)
    def hangup(self, call_id: int = -1) -> None:
        """End ONE call. Phase-2 multi-call: every card passes its own
        call_id so End never lands on the wrong leg (same teardown-race
        rationale as PhoneShell._pjsua_call_for)."""
        rec = self._record_for(call_id)
        if rec is None:
            return
        try:
            self._phone._on_hangup_by_id(rec.call_id)
        except Exception:
            log.exception("bridge.hangup failed")

    @Slot(int)
    def answer(self, call_id: int = -1) -> None:
        rec = self._record_for(call_id)
        if rec is None:
            return
        try:
            self._phone._on_answer(rec.call_id)
        except Exception:
            log.exception("bridge.answer failed")

    @Slot(int)
    def reject(self, call_id: int = -1) -> None:
        rec = self._record_for(call_id)
        if rec is None:
            return
        try:
            self._phone._on_reject(rec.call_id)
        except Exception:
            log.exception("bridge.reject failed")

    @Slot(int)
    def select_call(self, call_id: int) -> None:
        """Clicking a card promotes that call to selected (audio focus) --
        the exact semantic of the Qt calls_strip row click."""
        try:
            if self._phone.calls.get(int(call_id)) is not None:
                self._phone._select_call(int(call_id))
        except Exception:
            log.exception("bridge.select_call failed")

    # ------------------------------------------------------------------
    # In-call controls (per-call)
    # ------------------------------------------------------------------
    @Slot(int)
    def toggle_mute(self, call_id: int = -1) -> None:
        rec = self._record_for(call_id)
        if rec is None:
            return
        try:
            self._phone._on_mute_toggled(rec.call_id, not bool(rec.muted))
        except Exception:
            log.exception("bridge.toggle_mute failed")

    @Slot(int)
    def toggle_hold(self, call_id: int = -1) -> None:
        rec = self._record_for(call_id)
        if rec is None:
            return
        try:
            from noc_beam.sip.call_manager import CallState

            if rec.state == CallState.HELD:
                self._phone._on_resume(rec.call_id)
            else:
                self._phone._on_hold(rec.call_id)
        except Exception:
            log.exception("bridge.toggle_hold failed")

    @Slot(int, str)
    def transfer(self, call_id: int, target: str) -> None:
        """Blind-transfer ONE call. The web UI collects the target (the Qt
        path used a QInputDialog); everything else mirrors
        PhoneShell._on_transfer, but per-card so multi-call transfer is
        unambiguous."""
        target = (target or "").strip()
        rec = self._record_for(call_id)
        if not target or rec is None:
            return
        try:
            from noc_beam.sip.endpoint import SipEndpoint

            call = SipEndpoint.instance().find_call(rec.call_id)
            if call is None:
                return
            SipEndpoint.instance().blind_transfer(
                call, target, account_id=self._phone._active_account_id
            )
            self._phone._set_status(f"Transferring to {target}…", "muted", transient=True)
        except Exception:
            log.exception("bridge.transfer failed")

    @Slot(int, str)
    def send_dtmf(self, call_id: int, digit: str) -> None:
        """Per-call in-call DTMF (phase-2 multi-call: every card's compact
        pad passes its own call_id, so tones unambiguously target that
        card's call). When idle the web UI builds the dial string itself
        (mirrors ui/phone_shell.py:_on_digit_pressed's split)."""
        digit = (digit or "").strip()
        rec = self._record_for(call_id)
        if not digit or rec is None:
            return
        try:
            from noc_beam.sip.endpoint import SipEndpoint

            call = SipEndpoint.instance().find_call(rec.call_id)
            if call is None:
                return
            acc_cfg = next(
                (a for a in self._phone.accounts if a.id == rec.account_id), None
            )
            if acc_cfg is None:
                acc_cfg = next(
                    (a for a in self._phone.accounts
                     if a.id == self._phone._active_account_id),
                    None,
                )
            if acc_cfg is None:
                return
            SipEndpoint.instance().send_dtmf(call, digit, acc_cfg)
        except Exception:
            log.exception("bridge.send_dtmf failed")

    # ------------------------------------------------------------------
    # Account + supplier selection
    # ------------------------------------------------------------------
    @Slot(str)
    def select_account(self, account_id: str) -> None:
        if not account_id:
            return
        try:
            label = self._phone._account_label(account_id)
            self._phone._set_active_account(account_id, label)
        except Exception:
            log.exception("bridge.select_account failed")
        self._push_account_supplier()

    @Slot(str)
    def select_supplier(self, supplier_id: str) -> None:
        supplier_id = str(supplier_id or "")
        try:
            combo = getattr(self._phone, "supplier_combo", None)
            if combo is None:
                return
            idx = combo.findData(supplier_id)
            if idx >= 0:
                # setCurrentIndex fires currentIndexChanged ->
                # PhoneShell._on_supplier_changed, which does the real
                # Teles auth-swap / re-register work.
                combo.setCurrentIndex(idx)
            else:
                self._phone._active_supplier_id = supplier_id
        except Exception:
            log.exception("bridge.select_supplier failed")
        self._push_account_supplier()

    @Slot(str)
    def edit_account(self, account_id: str) -> None:
        """Open the account editor for ONE account -- the per-row gear on the
        web chrome's account dropdown (owner feedback 2026-07-16.3). Routes to
        PhoneShell._edit_account_by_id, the same modal the Qt Accounts window's
        per-row Edit uses, so the delete-in-call guard + re-register handling
        are shared. Row click still switches the active account; the gear edits
        THAT account without switching (stopPropagation on the JS side)."""
        account_id = (account_id or "").strip()
        if not account_id:
            return
        try:
            self._phone._edit_account_by_id(account_id)
        except Exception:
            log.exception("bridge.edit_account failed")
        # The editor is modal (blocks above); once it returns a renamed or
        # re-registered account must show through the pill + dropdown.
        self._push_account_supplier()

    def _push_account_supplier(self) -> None:
        try:
            pusher = getattr(self._web, "push_account_and_supplier", None)
            if callable(pusher):
                pusher()
        except Exception:
            log.exception("bridge push after selection failed")

    # ------------------------------------------------------------------
    # Auxiliary Qt windows (stay Qt in phase 1) + Quit
    # ------------------------------------------------------------------
    @Slot(str)
    def open_window(self, name: str) -> None:
        name = (name or "").strip().lower()
        try:
            routes = {
                "settings": self._phone._on_settings,
                "accounts": self._phone._on_open_accounts,
                "trace": self._phone._on_open_trace,
                "test-runner": self._phone._on_open_test_runner,
                "testrunner": self._phone._on_open_test_runner,
                "diagnostics": self._phone._on_diagnostics,
                "history": self._web.open_history,
                "contacts": self._web.open_contacts,
                "favorites": self._web.open_favorites,
                "quit": self._phone._on_quit,
            }
            handler = routes.get(name)
            if handler is not None:
                handler()
            else:
                log.warning("bridge.open_window: unknown target %r", name)
        except Exception:
            log.exception("bridge.open_window(%s) failed", name)

    # ------------------------------------------------------------------
    # Window chrome (frameless)
    # ------------------------------------------------------------------
    @Slot()
    def minimize(self) -> None:
        try:
            self._web.showMinimized()
        except Exception:
            log.exception("bridge.minimize failed")

    @Slot()
    def close_win(self) -> None:
        try:
            self._web.close()
        except Exception:
            log.exception("bridge.close_win failed")

    @Slot(str)
    def set_drag_zones(self, payload: str) -> None:
        """The page reports the title-bar band + the rectangles of its
        interactive controls (account pill, menu, window buttons) as JSON
        `{"band": h, "exclude": [{x,y,w,h}, ...]}` in CSS px. WebShell
        hit-tests mouse presses against these synchronously so the OS move
        starts on the very first pixel instead of after the async
        JS -> QWebChannel -> start_move() round trip that made dragging feel
        laggy (owner feedback 2026-09-09)."""
        try:
            import json

            self._web.set_drag_zones(json.loads(payload or "{}"))
        except Exception:
            log.debug("bridge.set_drag_zones ignored", exc_info=True)

    @Slot()
    def start_move(self) -> None:
        try:
            handle = self._web.windowHandle()
            if handle is not None:
                handle.startSystemMove()
        except Exception:
            log.exception("bridge.start_move failed")

    @Slot()
    def toggle_max_restore(self) -> None:
        try:
            if self._web.isMaximized():
                self._web.showNormal()
            else:
                self._web.showMaximized()
        except Exception:
            log.exception("bridge.toggle_max_restore failed")

    # ------------------------------------------------------------------
    # Web views (phase 2): on-demand data refresh when a tab opens
    # ------------------------------------------------------------------
    @Slot()
    def refresh_history(self) -> None:
        try:
            self._web.push_history()
        except Exception:
            log.exception("bridge.refresh_history failed")

    @Slot()
    def refresh_contacts(self) -> None:
        try:
            self._web.push_contacts()
        except Exception:
            log.exception("bridge.refresh_contacts failed")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @Slot()
    def ready(self) -> None:
        """The page calls this once the QWebChannel handshake completes;
        WebShell responds with the initial full state push."""
        try:
            self._web.push_all()
        except Exception:
            log.exception("bridge.ready push failed")

    @Slot(str)
    def log(self, msg: str) -> None:
        log.info("[webui] %s", msg)
