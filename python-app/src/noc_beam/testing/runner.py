from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

from PySide6.QtCore import QObject, QTimer, Signal

from noc_beam.config.store import AccountConfig
from noc_beam.sip.endpoint import SipEndpoint
from noc_beam.sip.events import SipEvents, sip_events
from noc_beam.testing.plan import TestCall, TestSpec, expand


@dataclass
class TestResult:
    call: TestCall
    result: Literal["PASS", "FAIL"]
    sip_code: int | None
    sip_reason: str
    rtt_ms: float | None
    duration_s: float
    notes: str
    started_at: float
    from_account: str
    to_uri: str


@dataclass
class _ActiveCall:
    call: TestCall
    account: AccountConfig
    target_uri: str
    sip_call: object
    call_id: int
    started_at_mono: float
    started_at_wall: float
    timeout_timer: QTimer
    hold_timer: QTimer | None = None
    cleanup_timer: QTimer | None = None
    rtt_ms: float | None = None
    completed: bool = False


CLEANUP_FALLBACK_SECONDS = 1.0


class TestRunner(QObject):
    call_started = Signal(int)
    call_completed = Signal(object)
    run_complete = Signal(object)

    def __init__(
        self,
        spec: TestSpec,
        accounts: list[AccountConfig],
        parent: QObject | None = None,
        *,
        endpoint: object | None = None,
        events: SipEvents | None = None,
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        self.accounts = accounts
        self.endpoint = endpoint if endpoint is not None else SipEndpoint.instance()
        self.events = events if events is not None else sip_events()

        self._queue: deque[TestCall] = deque()
        self._active: dict[int, _ActiveCall] = {}
        self._closing_slots: dict[int, QTimer] = {}
        self._next_closing_slot = 1
        self._results: list[TestResult] = []
        self._started = False
        self._cancelled = False
        self._run_complete_emitted = False

        self.events.call_state_changed.connect(self._on_call_state_changed)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._queue = deque(expand(self.spec))
        self._fill_slots()
        self._maybe_emit_run_complete()

    def cancel(self) -> None:
        if self._run_complete_emitted:
            return
        self._cancelled = True

        for active in list(self._active.values()):
            self._complete_active(
                active,
                "FAIL",
                0,
                "Cancelled",
                "cancelled",
                hangup=True,
                fill_slots=False,
            )

        while self._queue:
            call = self._queue.popleft()
            account = self._resolve_account(call.caller_number)
            target_uri = (
                self._build_target_uri(call.target_number, account)
                if account is not None
                else call.target_number
            )
            self._emit_result(
                call=call,
                result="FAIL",
                sip_code=0,
                sip_reason="Cancelled",
                rtt_ms=None,
                duration_s=0.0,
                notes="cancelled",
                started_at=time.time(),
                from_account=account.id if account is not None else call.caller_number,
                to_uri=target_uri,
            )

        self._maybe_emit_run_complete()

    def _fill_slots(self) -> None:
        """Dispatch the NEXT call, then re-arm via QTimer.singleShot.

        Original tight while-loop blocked the Qt main thread for the
        whole batch (parallel=16 * slow registrar = GUI frozen for
        seconds). Despite the docstring claiming otherwise, the body
        was still a synchronous while. Now we dispatch at most one
        call per invocation and re-post _fill_slots to the event loop
        via QTimer.singleShot when there's still room AND queue, so
        Qt gets a tick between each pjsua2.makeCall.
        """
        if self._cancelled:
            return
        # Re-entrancy guard: processEvents() inside the dispatch loop
        # below can run user-driven events that re-enter _fill_slots
        # (e.g. the Stop button calls cancel() which iterates _active
        # while we're mid-dispatch -- race condition that could
        # drop calls or double-hangup).
        if getattr(self, "_dispatching", False):
            return
        self._dispatching = True
        try:
            # Synchronously dispatch what fits in the parallel budget
            # but pump the Qt event loop between each call so signal
            # callbacks (call_started, registration_changed) and UI
            # repaints land mid-batch. Original code did neither,
            # freezing the GUI for the whole burst at parallel=16.
            while self._queue and self._slots_in_use() < self.spec.parallel:
                if self._cancelled:
                    return
                self._dispatch_one_call()
                self._yield_to_event_loop()
        finally:
            self._dispatching = False

    @staticmethod
    def _yield_to_event_loop() -> None:
        try:
            from PySide6.QtCore import QCoreApplication
            app = QCoreApplication.instance()
            if app is not None:
                app.processEvents()
        except Exception:
            pass

    def _dispatch_one_call(self) -> None:
        call = self._queue.popleft()
        account = self._resolve_account(call.caller_number)
        started_at_mono = time.monotonic()
        started_at_wall = time.time()
        if account is None:
            self._emit_result(
                call=call,
                result="FAIL",
                sip_code=0,
                sip_reason="No matching account",
                rtt_ms=None,
                duration_s=0.0,
                notes="no matching account",
                started_at=started_at_wall,
                from_account=call.caller_number,
                to_uri=call.target_number,
            )
            return

        target_uri = self._build_target_uri(call.target_number, account)
        try:
            sip_call = self.endpoint.make_call(account.id, target_uri)
        except Exception as exc:
            text = str(exc)
            notes = (
                "pjsua2 not available"
                if "pjsua2 not available" in text.lower()
                else text
            )
            self._emit_result(
                call=call,
                result="FAIL",
                sip_code=0,
                sip_reason="Endpoint error",
                rtt_ms=None,
                duration_s=time.monotonic() - started_at_mono,
                notes=notes,
                started_at=started_at_wall,
                from_account=account.id,
                to_uri=target_uri,
            )
            return

        try:
            call_id = int(sip_call.getInfo().id)
        except Exception as exc:
            text = str(exc)
            notes = (
                "pjsua2 not available"
                if "pjsua2 not available" in text.lower()
                else text
            )
            self._hold_closing_slot()
            self._emit_result(
                call=call,
                result="FAIL",
                sip_code=0,
                sip_reason="Endpoint error",
                rtt_ms=None,
                duration_s=time.monotonic() - started_at_mono,
                notes=notes,
                started_at=started_at_wall,
                from_account=account.id,
                to_uri=target_uri,
            )
            self._hangup(sip_call)
            return

        timeout_timer = self._make_timer(self.spec.timeout_seconds)
        active = _ActiveCall(
            call=call,
            account=account,
            target_uri=target_uri,
            sip_call=sip_call,
            call_id=call_id,
            started_at_mono=started_at_mono,
            started_at_wall=started_at_wall,
            timeout_timer=timeout_timer,
        )
        self._active[call_id] = active
        timeout_timer.timeout.connect(lambda cid=call_id: self._on_timeout(cid))
        timeout_timer.start()
        self.call_started.emit(call.index)

    def _on_call_state_changed(
        self,
        account_id: str,
        call_id: int,
        state: str,
        code: int,
        reason: str,
    ) -> None:
        active = self._active.get(call_id)
        if active is None or active.account.id != account_id:
            return

        if state == "DISCONNECTED" and active.completed:
            self._release_active(active)
            return
        if active.completed:
            return

        now = time.monotonic()
        if code > 0 and active.rtt_ms is None:
            active.rtt_ms = (now - active.started_at_mono) * 1000.0

        # 401 Unauthorized / 407 Proxy Auth Required: PJSIP transparently
        # re-INVITEs with the digest credentials. Treating the initial
        # challenge as FAIL caused EVERY auth-challenged account to mis-
        # report on the first attempt before the retry could land.
        # Same for 100 Trying and other 1xx informational that aren't
        # already handled below.
        if code in (401, 407):
            return
        if 400 <= code <= 699:
            self._complete_active(active, "FAIL", code, reason, reason)
            if state == "DISCONNECTED":
                self._release_active(active)
            return

        if (
            self.spec.pass_criterion == "reachability"
            and state == "EARLY"
            and code in (180, 183)
        ):
            self._complete_active(active, "PASS", code, reason, "")
            return

        if self.spec.pass_criterion == "full-call" and state == "CONFIRMED":
            if active.hold_timer is not None:
                return
            hold_timer = self._make_timer(self.spec.hold_seconds)
            active.hold_timer = hold_timer
            hold_timer.timeout.connect(
                lambda cid=call_id, c=code, r=reason: self._on_hold_complete(cid, c, r)
            )
            hold_timer.start()
            return

        if state == "DISCONNECTED":
            self._complete_active(
                active,
                "FAIL",
                code or 0,
                reason or "Disconnected",
                reason or "Disconnected",
            )
            self._release_active(active)

    def _on_hold_complete(self, call_id: int, code: int, reason: str) -> None:
        active = self._active.get(call_id)
        if active is None:
            return
        self._complete_active(active, "PASS", code or 200, reason or "OK", "")

    def _on_timeout(self, call_id: int) -> None:
        active = self._active.get(call_id)
        if active is None:
            return
        self._complete_active(
            active,
            "FAIL",
            408,
            "Request Timeout",
            "timeout",
            hangup=True,
        )

    def _complete_active(
        self,
        active: _ActiveCall,
        result: Literal["PASS", "FAIL"],
        sip_code: int | None,
        sip_reason: str,
        notes: str,
        *,
        hangup: bool = True,
        fill_slots: bool = True,
    ) -> None:
        if self._active.get(active.call_id) is not active or active.completed:
            return

        active.completed = True
        active.timeout_timer.stop()
        if active.hold_timer is not None:
            active.hold_timer.stop()
        self._emit_result(
            call=active.call,
            result=result,
            sip_code=sip_code,
            sip_reason=sip_reason,
            rtt_ms=active.rtt_ms,
            duration_s=time.monotonic() - active.started_at_mono,
            notes=notes,
            started_at=active.started_at_wall,
            from_account=active.account.id,
            to_uri=active.target_uri,
        )

        if hangup:
            self._hangup(active.sip_call)

        if self._active.get(active.call_id) is active:
            self._start_cleanup_timer(active, fill_slots=fill_slots)

    def _release_active(self, active: _ActiveCall, *, fill_slots: bool = True) -> None:
        if self._active.pop(active.call_id, None) is None:
            return
        active.timeout_timer.stop()
        if active.hold_timer is not None:
            active.hold_timer.stop()
        if active.cleanup_timer is not None:
            active.cleanup_timer.stop()
        if fill_slots:
            self._fill_slots()
        self._maybe_emit_run_complete()

    def _start_cleanup_timer(
        self,
        active: _ActiveCall,
        *,
        fill_slots: bool = True,
    ) -> None:
        cleanup_timer = self._make_timer(CLEANUP_FALLBACK_SECONDS)
        active.cleanup_timer = cleanup_timer
        cleanup_timer.timeout.connect(
            lambda a=active, fs=fill_slots: self._release_active(a, fill_slots=fs)
        )
        cleanup_timer.start()

    def _hold_closing_slot(self) -> None:
        token = self._next_closing_slot
        self._next_closing_slot += 1
        cleanup_timer = self._make_timer(CLEANUP_FALLBACK_SECONDS)
        self._closing_slots[token] = cleanup_timer
        cleanup_timer.timeout.connect(lambda t=token: self._release_closing_slot(t))
        cleanup_timer.start()

    def _release_closing_slot(self, token: int) -> None:
        timer = self._closing_slots.pop(token, None)
        if timer is None:
            return
        timer.stop()
        self._fill_slots()
        self._maybe_emit_run_complete()

    def _slots_in_use(self) -> int:
        return len(self._active) + len(self._closing_slots)

    def _emit_result(
        self,
        *,
        call: TestCall,
        result: Literal["PASS", "FAIL"],
        sip_code: int | None,
        sip_reason: str,
        rtt_ms: float | None,
        duration_s: float,
        notes: str,
        started_at: float,
        from_account: str,
        to_uri: str,
    ) -> None:
        test_result = TestResult(
            call=call,
            result=result,
            sip_code=sip_code,
            sip_reason=sip_reason,
            rtt_ms=rtt_ms,
            duration_s=duration_s,
            notes=notes,
            started_at=started_at,
            from_account=from_account,
            to_uri=to_uri,
        )
        self._results.append(test_result)
        self.call_completed.emit(test_result)

    def _maybe_emit_run_complete(self) -> None:
        if self._run_complete_emitted:
            return
        if self._queue or self._active or self._closing_slots:
            return
        self._run_complete_emitted = True
        # CRITICAL: disconnect from the singleton sip_events so this
        # runner doesn't keep processing call_state on a future run.
        # Without this every Test Runner Run leaks a permanent slot
        # and the n-th run gets n duplicates routed at it -- old
        # runners process new call IDs and corrupt the result table.
        try:
            self.events.call_state_changed.disconnect(self._on_call_state_changed)
        except Exception:
            pass
        self.run_complete.emit(list(self._results))

    def _make_timer(self, seconds: float) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(max(1, int(seconds * 1000)))
        return timer

    def _resolve_account(self, caller_number: str) -> AccountConfig | None:
        for account in self.accounts:
            if account.username == caller_number:
                return account
        return None

    @staticmethod
    def _build_target_uri(target: str, account: AccountConfig) -> str:
        if target.startswith(("sip:", "sips:", "tel:")):
            return target
        if "@" in target:
            return f"sip:{target}"
        return f"sip:{target}@{account.domain}"

    def _hangup(self, call: object) -> None:
        try:
            self.endpoint.hangup_call(call)
        except Exception:
            pass
