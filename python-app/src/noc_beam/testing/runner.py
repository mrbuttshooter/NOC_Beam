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
    # FAS verdict captured at end of call (empty if call never reached
    # CONFIRMED -- e.g. 408 / 4xx where no audio flowed). Populated from
    # CallRecord.fas_verdict at result-finalisation time.
    fas_verdict: str = ""
    fas_confidence: float = 0.0
    fas_reasons: str = ""


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
    # Reachability pass-criterion bookkeeping: we now require an
    # actual 180 Ringing before declaring success. 183 Session
    # Progress (or any other 1xx without subsequent 180) is treated
    # as early-media-only and FAILED at timeout — many wholesale
    # carriers send 183 + canned "all circuits busy" recordings that
    # tone-sound like a real ringback but never deliver a call.
    saw_180: bool = False
    saw_other_1xx: bool = False


CLEANUP_FALLBACK_SECONDS = 1.0


def _render_supplier_template(template: str, supplier_id: str) -> str:
    rendered = (template or "").strip()
    for token in ("{id}", "{ID}", "{Id}", "{iD}"):
        rendered = rendered.replace(token, supplier_id)
    return rendered


def _genband_supplier_prefix(account: AccountConfig, supplier_id: str) -> str:
    supplier_id = str(supplier_id or "").strip()
    dial_prefix = (getattr(account, "dial_prefix", "") or "").strip()
    routing_fmt = (getattr(account, "routing_format", "") or "").strip()
    if supplier_id:
        if "{id}" in routing_fmt.lower():
            return _render_supplier_template(routing_fmt, supplier_id)
        if "{id}" in dial_prefix.lower():
            return _render_supplier_template(dial_prefix, supplier_id)
        if routing_fmt:
            return routing_fmt
        return f"{dial_prefix}{supplier_id}"
    return _render_supplier_template(dial_prefix, supplier_id)


def _apply_routing_to_target(
    target: str,
    account: AccountConfig,
    supplier_id: str = "",
) -> str:
    """Mirror PhoneShell._rewrite_dial_target for batch dispatch.

    Prepends:
      1. supplier prefix (Genband only, from the explicit `supplier_id`
         argument — was previously read from a `_active_supplier_id`
         attribute stamped onto the shared AccountConfig by the caller,
         which leaked stale state across runs and risked contaminating
         the on-disk accounts.json schema)
      2. account dial_prefix

    SIP URIs and anything containing '@' are short-circuited above.
    """
    if not target:
        return target
    out = target
    kind = (getattr(account, "switch_type", "other") or "other").lower()
    if kind == "genband":
        prefix = _genband_supplier_prefix(account, supplier_id)
        if prefix and not out.startswith(prefix):
            out = f"{prefix}{out}"
    else:
        dial_prefix = (getattr(account, "dial_prefix", "") or "").strip()
        if dial_prefix and not out.startswith(dial_prefix):
            out = f"{dial_prefix}{out}"
    return out


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
        supplier_id: str = "",
        active_account_id: str = "",
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        self.accounts = accounts
        self.endpoint = endpoint if endpoint is not None else SipEndpoint.instance()
        self.events = events if events is not None else sip_events()
        # Supplier picked for this batch (Genband prefix routing).
        # Stored on the runner instance, not stamped onto each
        # AccountConfig — the older `setattr(acc, "_active_supplier_id",
        # ...)` pattern leaked stale state into shared mutable accounts
        # that also persist to disk.
        self._supplier_id = supplier_id or ""
        self._active_account_id = active_account_id or ""

        self._queue: deque[TestCall] = deque()
        self._active: dict[int, _ActiveCall] = {}
        self._closing_slots: dict[int, QTimer] = {}
        self._next_closing_slot = 1
        self._results: list[TestResult] = []
        self._started = False
        self._cancelled = False
        # Re-entrancy guard for _fill_slots; declared in __init__ so
        # subclasses overriding __init__ that forget super().__init__
        # still get a defined attribute and the guard short-circuits
        # correctly. Was previously created lazily via getattr.
        self._dispatching = False
        self._run_complete_emitted = False
        # Cache of the last FAS verdict seen per call_id, so we can write
        # the verdict into TestResult at completion time (CallManager
        # record is dropped on DISCONNECTED before we finalise).
        self._fas_by_call_id: dict[int, tuple[str, float, str]] = {}
        try:
            from noc_beam.config.store import load_settings

            self._auto_pause_on_fas_count = int(load_settings().fas.auto_pause_on_fas_count)
        except Exception:
            self._auto_pause_on_fas_count = 0
        self._consecutive_likely_fas = 0

        self.events.call_state_changed.connect(self._on_call_state_changed)
        try:
            self.events.call_fas_verdict.connect(self._on_fas_verdict)
        except Exception:
            pass  # older SipEvents builds without the signal

    def _on_fas_verdict(self, call_id: int, verdict: str, confidence: float, reasons: str) -> None:
        """Cache the latest FAS verdict per call so completion-time
        TestResult records it even after the CallRecord is gone."""
        if call_id not in self._active:
            return
        self._fas_by_call_id[call_id] = (verdict, float(confidence), reasons or "")

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
        # drop calls or double-hangup). _dispatching is initialised
        # in __init__ so the bare attribute read is safe.
        if self._dispatching:
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
            sip_call = self.endpoint.make_call(
                account.id,
                target_uri,
                origin="test_runner",
                origin_meta={
                    "supplier_id": self._supplier_id,
                },
            )
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

        # Reachability pass criterion: tightened semantics.
        #
        # Previously ANY 1xx (180 or 183) in EARLY passed. That over-
        # counted carriers that send 183 + "this number is not in
        # service" recordings as reachable. New rules (per LOGIC_REVIEW
        # 2026-05-25):
        #   180 Ringing in EARLY -> PASS reason "180_then_bye"
        #     (we hangup right away -- destination sees a missed call).
        #   183 (or other 1xx) in EARLY -> note it, but do NOT pass.
        #     Wait for 180 or 200 or timeout.
        #   200 in CONFIRMED after 180 -> PASS reason "answered".
        #   200 in CONFIRMED without prior 180 -> PASS reason
        #     "200_no_180_warn" (some endpoints skip 180).
        if self.spec.pass_criterion == "reachability":
            if state == "EARLY":
                if code == 180:
                    active.saw_180 = True
                    self._complete_active(
                        active, "PASS", code, reason, "180_then_bye"
                    )
                    return
                if 100 <= code < 200:
                    # 183 Session Progress, 100 Trying, 181/182, etc.
                    # Record that we saw early media so timeout reason
                    # is "early_media_only_no_180", not bare "timeout".
                    active.saw_other_1xx = True
                    return
            if state == "CONFIRMED" and code == 200:
                reason_code = "answered" if active.saw_180 else "200_no_180_warn"
                self._complete_active(active, "PASS", code, reason, reason_code)
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
        # Reachability: if we got 183/100/etc. but never a 180, surface
        # "early_media_only_no_180" so downstream analysts can spot
        # carriers that advertise early media without ever ringing the
        # destination (a common pattern for SIT/IVR call-progress fraud).
        if (
            self.spec.pass_criterion == "reachability"
            and active.saw_other_1xx
            and not active.saw_180
        ):
            notes = "early_media_only_no_180"
        else:
            notes = "timeout"
        self._complete_active(
            active,
            "FAIL",
            408,
            "Request Timeout",
            notes,
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
        test_result = self._emit_result(
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
            sip_call_id=active.call_id,
        )
        self._maybe_auto_pause_for_fas(test_result)

        if hangup:
            self._hangup(active.sip_call)

        if self._active.get(active.call_id) is active:
            self._start_cleanup_timer(active, fill_slots=fill_slots)

    def _release_active(self, active: _ActiveCall, *, fill_slots: bool = True) -> None:
        if self._active.pop(active.call_id, None) is None:
            return
        self._fas_by_call_id.pop(active.call_id, None)
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
        sip_call_id: int = -1,
    ) -> TestResult:
        # Look up the last FAS verdict seen for this call_id (cached by
        # _on_fas_verdict). If the call never reached CONFIRMED no verdict
        # was emitted; fields stay empty and the runner UI renders "—".
        fas_verdict = ""
        fas_confidence = 0.0
        fas_reasons = ""
        cached = self._fas_by_call_id.pop(sip_call_id, None)
        if cached:
            fas_verdict, fas_confidence, fas_reasons = cached

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
            fas_verdict=fas_verdict,
            fas_confidence=fas_confidence,
            fas_reasons=fas_reasons,
        )
        self._results.append(test_result)
        self.call_completed.emit(test_result)
        return test_result

    def _maybe_auto_pause_for_fas(self, result: TestResult) -> None:
        threshold = self._auto_pause_on_fas_count
        if threshold <= 0 or self._cancelled:
            return
        if result.fas_verdict in {"LIKELY_FAS", "PROBABLE_FAS", "CONFIRMED_FAS"}:
            self._consecutive_likely_fas += 1
        else:
            self._consecutive_likely_fas = 0
            return
        if self._consecutive_likely_fas < threshold:
            return

        self._cancelled = True
        note = f"auto-paused after {self._consecutive_likely_fas} consecutive FAS verdicts"
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
                sip_reason="Auto-paused",
                rtt_ms=None,
                duration_s=0.0,
                notes=note,
                started_at=time.time(),
                from_account=account.id if account is not None else call.caller_number,
                to_uri=target_uri,
            )

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
        """Resolve a caller token into a registered AccountConfig.

        Acceptance order:
          1. Blank / "*" / "auto" -> first enabled account (most common
             demo case: user pastes a target list, doesn't fill callers,
             expects "use my one account")
          2. Exact username match
          3. Exact id match (account UUID)
          4. account_id match (for multi-account dispatch where the
             caller column carries the account selector)
        Previously this required exact username equality only, so
        a user who pasted a list of numbers into the callers field
        (e.g. their own dial-out numbers, not the account username)
        got "no matching account" on every row -- silent total
        failure across the whole Test Runner job.
        """
        if not self.accounts:
            return None
        token = (caller_number or "").strip()
        # Empty / wildcard -> first enabled account
        if not token or token in ("*", "auto", "any"):
            if self._active_account_id:
                for account in self.accounts:
                    if (
                        getattr(account, "id", None) == self._active_account_id
                        and getattr(account, "enabled", True)
                    ):
                        return account
            for account in self.accounts:
                if getattr(account, "enabled", True):
                    return account
            return self.accounts[0]
        for account in self.accounts:
            if account.username == token:
                return account
        for account in self.accounts:
            if getattr(account, "id", None) == token:
                return account
        return None

    def _build_target_uri(self, target: str, account: AccountConfig) -> str:
        if target.startswith(("sip:", "sips:", "tel:")):
            return target
        if "@" in target:
            return f"sip:{target}"
        # Apply account-level dial prefix and (for Genband) the active
        # supplier's routed prefix. The active supplier id is set on the
        # account-bound runner -- if absent we just prepend dial_prefix.
        target = _apply_routing_to_target(target, account, self._supplier_id)
        return f"sip:{target}@{account.domain}"

    def _hangup(self, call: object) -> None:
        try:
            self.endpoint.hangup_call(call)
        except Exception:
            pass
