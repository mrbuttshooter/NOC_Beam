from __future__ import annotations

import time
from collections.abc import Callable

import pytest

QtCore = pytest.importorskip("PySide6.QtCore")
QCoreApplication = QtCore.QCoreApplication
QEventLoop = QtCore.QEventLoop
QTimer = QtCore.QTimer

from noc_beam.config.store import AccountConfig
from noc_beam.sip.events import SipEvents
from noc_beam.testing import runner as runner_module
from noc_beam.testing.plan import TestSpec as RunnerSpec
from noc_beam.testing.runner import TestResult as RunnerResult
from noc_beam.testing.runner import TestRunner as Runner


def ensure_app() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture(autouse=True)
def qt_app() -> QCoreApplication:
    return ensure_app()


class StubInfo:
    def __init__(self, call_id: int) -> None:
        self.id = call_id


class StubCall:
    def __init__(self, call_id: int) -> None:
        self._info = StubInfo(call_id)

    def getInfo(self) -> StubInfo:  # noqa: N802
        return self._info


class BrokenInfoCall:
    def getInfo(self) -> StubInfo:  # noqa: N802
        raise RuntimeError("getInfo failed")


class StubEndpoint:
    def __init__(self) -> None:
        self.next_call_id = 100
        self.calls: dict[int, tuple[str, str, StubCall]] = {}
        self.hung_up: list[object] = []
        self.max_active = 0

    def make_call(self, account_id: str, target_uri: str, **_kwargs) -> StubCall:
        call = StubCall(self.next_call_id)
        self.next_call_id += 1
        self.calls[call.getInfo().id] = (account_id, target_uri, call)
        self.max_active = max(self.max_active, len(self.calls))
        return call

    def hangup_call(self, call: object) -> None:
        self.hung_up.append(call)

    def release_call(self, call_id: int) -> None:
        self.calls.pop(call_id, None)


def account(username: str = "1001", account_id: str = "acc-1") -> AccountConfig:
    return AccountConfig(
        id=account_id,
        username=username,
        domain="pbx.example.test",
    )


def test_routing_does_not_double_prepend_existing_dial_prefix() -> None:
    acc = account()
    acc.dial_prefix = "00"

    assert runner_module._apply_routing_to_target("96171488860", acc) == "0096171488860"
    assert runner_module._apply_routing_to_target("0096171488860", acc) == "0096171488860"


def spec(
    *,
    callers: list[str] | None = None,
    targets: list[str] | None = None,
    pass_criterion: str = "reachability",
    parallel: int = 1,
    hold_seconds: float = 0.01,
    timeout_seconds: float = 0.2,
) -> RunnerSpec:
    return RunnerSpec(
        callers=callers or ["1001"],
        targets=targets or ["2001"],
        mode="paired",
        pass_criterion=pass_criterion,  # type: ignore[arg-type]
        parallel=parallel,
        hold_seconds=hold_seconds,
        timeout_seconds=timeout_seconds,
    )


def wait_until(predicate: Callable[[], bool], timeout_ms: int = 1000) -> None:
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while not predicate() and time.monotonic() < deadline:
        loop = QEventLoop()
        QTimer.singleShot(5, loop.quit)
        loop.exec()
    assert predicate()


def first_call_id(endpoint: StubEndpoint) -> int:
    return next(iter(endpoint.calls))


def emit_state(
    events: SipEvents,
    endpoint: StubEndpoint,
    call_id: int,
    state: str,
    code: int,
    reason: str,
    account_id: str = "acc-1",
) -> None:
    if state == "DISCONNECTED":
        endpoint.release_call(call_id)
    events.call_state_changed.emit(account_id, call_id, state, code, reason)


def wait_for_completed(results: list[RunnerResult], count: int = 1) -> None:
    wait_until(lambda: len(results) >= count, timeout_ms=500)


def test_reachability_passes_on_first_180_ringing() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)

    results: list[RunnerResult] = []
    completed_runs: list[list[RunnerResult]] = []
    runner.call_completed.connect(results.append)
    runner.run_complete.connect(completed_runs.append)
    runner.start()

    call_id = first_call_id(endpoint)
    call = endpoint.calls[call_id][2]
    emit_state(events, endpoint, call_id, "EARLY", 180, "Ringing")

    assert len(results) == 1
    assert results[0].result == "PASS"
    assert results[0].sip_code == 180
    assert results[0].sip_reason == "Ringing"
    assert results[0].to_uri == "sip:2001@pbx.example.test"
    assert endpoint.hung_up == [call]
    assert completed_runs == []

    emit_state(events, endpoint, call_id, "DISCONNECTED", 487, "Request Terminated")
    assert len(completed_runs) == 1


def test_result_started_at_uses_wall_clock_while_timings_use_monotonic() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)

    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)

    before = time.time()
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, call_id, "EARLY", 180, "Ringing")
    after = time.time()

    assert len(results) == 1
    assert before <= results[0].started_at <= after
    assert results[0].duration_s >= 0.0
    assert results[0].rtt_ms is not None
    assert results[0].rtt_ms >= 0.0


def test_full_call_passes_after_200_ok_and_hold_timer_expiry() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(
        spec(pass_criterion="full-call", hold_seconds=0.01),
        [account()],
        endpoint=endpoint,
        events=events,
    )

    results: list[RunnerResult] = []
    completed_runs: list[list[RunnerResult]] = []
    runner.call_completed.connect(results.append)
    runner.run_complete.connect(completed_runs.append)
    runner.start()

    call_id = first_call_id(endpoint)
    call = endpoint.calls[call_id][2]
    emit_state(events, endpoint, call_id, "CONFIRMED", 200, "OK")
    wait_for_completed(results)

    assert results[0].result == "PASS"
    assert results[0].sip_code == 200
    assert results[0].sip_reason == "OK"
    assert endpoint.hung_up == [call]
    assert completed_runs == []

    emit_state(events, endpoint, call_id, "DISCONNECTED", 200, "OK")
    assert len(completed_runs) == 1


def test_fails_on_404() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)

    results: list[RunnerResult] = []
    runner.run_complete.connect(lambda emitted: results.extend(emitted))
    runner.start()

    call_id = first_call_id(endpoint)
    call = endpoint.calls[call_id][2]
    emit_state(events, endpoint, call_id, "DISCONNECTED", 404, "Not Found")

    assert len(results) == 1
    assert results[0].result == "FAIL"
    assert results[0].sip_code == 404
    assert results[0].sip_reason == "Not Found"
    assert endpoint.hung_up == [call]


def test_fails_without_matching_account() -> None:
    # A non-numeric token that names no account still fails without dialling.
    # (A phone number is an ORIGINATION A-number -- see the test below.)
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(
        spec(callers=["no-such-user"]),
        [account()],
        endpoint=endpoint,
        events=events,
    )

    results: list[RunnerResult] = []
    runner.run_complete.connect(lambda emitted: results.extend(emitted))
    runner.start()
    wait_until(lambda: len(results) == 1, timeout_ms=500)

    assert len(results) == 1
    assert results[0].result == "FAIL"
    assert results[0].sip_code == 0
    assert results[0].sip_reason == "No matching account"
    assert results[0].notes == "no matching account"
    assert endpoint.calls == {}


def test_fails_on_timeout() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    timeout_spec = spec(timeout_seconds=0.1)
    timeout_spec.timeout_seconds = 0.01
    runner = Runner(timeout_spec, [account()], endpoint=endpoint, events=events)

    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()

    call_id = first_call_id(endpoint)
    call = endpoint.calls[call_id][2]
    wait_for_completed(results)

    assert results[0].result == "FAIL"
    assert results[0].sip_code == 408
    assert results[0].sip_reason == "Request Timeout"
    assert results[0].notes == "timeout"
    assert endpoint.hung_up == [call]
    emit_state(events, endpoint, call_id, "DISCONNECTED", 408, "Request Timeout")


def test_parallel_run_never_exceeds_configured_concurrency() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(
        spec(
            callers=["1001", "1001", "1001", "1001"],
            targets=["2001", "2002", "2003", "2004"],
            parallel=2,
        ),
        [account()],
        endpoint=endpoint,
        events=events,
    )

    results: list[RunnerResult] = []
    runner.run_complete.connect(lambda emitted: results.extend(emitted))
    runner.start()

    assert len(endpoint.calls) == 2
    while len(results) < 4:
        call_id = first_call_id(endpoint)
        emit_state(events, endpoint, call_id, "EARLY", 180, "Ringing")
        emit_state(events, endpoint, call_id, "DISCONNECTED", 487, "Request Terminated")

    assert len(results) == 4
    assert endpoint.max_active == 2
    assert all(result.result == "PASS" for result in results)


def test_cancel_fails_in_flight_and_queued_calls() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(
        spec(
            callers=["1001", "1001", "1001", "1001"],
            targets=["2001", "2002", "2003", "2004"],
            parallel=2,
        ),
        [account()],
        endpoint=endpoint,
        events=events,
    )

    results: list[RunnerResult] = []
    runner.run_complete.connect(lambda emitted: results.extend(emitted))
    runner.start()

    active_call_ids = list(endpoint.calls)
    active_calls = [endpoint.calls[call_id][2] for call_id in active_call_ids]
    runner.cancel()

    for call_id in active_call_ids:
        emit_state(events, endpoint, call_id, "DISCONNECTED", 0, "Cancelled")

    assert len(results) == 4
    assert all(result.result == "FAIL" for result in results)
    assert all(result.sip_reason == "Cancelled" for result in results)
    assert all(result.notes == "cancelled" for result in results)
    assert endpoint.hung_up == active_calls
    assert endpoint.calls == {}


def test_reachability_holds_slot_until_disconnected_before_next_call() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(
        spec(
            callers=["1001", "1001"],
            targets=["2001", "2002"],
            parallel=1,
        ),
        [account()],
        endpoint=endpoint,
        events=events,
    )

    results: list[RunnerResult] = []
    started: list[int] = []
    runner.call_completed.connect(results.append)
    runner.call_started.connect(started.append)
    runner.start()

    first_id = first_call_id(endpoint)
    emit_state(events, endpoint, first_id, "EARLY", 180, "Ringing")

    assert len(results) == 1
    assert results[0].result == "PASS"
    assert started == [1]
    assert len(endpoint.calls) == 1
    assert first_id in endpoint.calls

    emit_state(events, endpoint, first_id, "DISCONNECTED", 487, "Request Terminated")

    assert started == [1, 2]
    assert len(endpoint.calls) == 1
    second_id = first_call_id(endpoint)
    assert second_id != first_id


class BrokenInfoEndpoint(StubEndpoint):
    def __init__(self) -> None:
        super().__init__()
        self.broken_call = BrokenInfoCall()

    def make_call(self, account_id: str, target_uri: str, **_kwargs) -> BrokenInfoCall:
        return self.broken_call


def test_getinfo_failure_after_make_call_hangs_up_returned_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "CLEANUP_FALLBACK_SECONDS", 0.01)
    events = SipEvents()
    endpoint = BrokenInfoEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)

    results: list[RunnerResult] = []
    runner.run_complete.connect(lambda emitted: results.extend(emitted))
    runner.start()
    wait_until(lambda: len(results) == 1, timeout_ms=500)

    assert len(results) == 1
    assert results[0].result == "FAIL"
    assert results[0].sip_code == 0
    assert results[0].sip_reason == "Endpoint error"
    assert results[0].notes == "getInfo failed"
    assert endpoint.hung_up == [endpoint.broken_call]


class SyncDisconnectEndpoint(StubEndpoint):
    def __init__(self, events: SipEvents) -> None:
        super().__init__()
        self.events = events

    def hangup_call(self, call: object) -> None:
        super().hangup_call(call)
        call_id = call.getInfo().id  # type: ignore[union-attr]
        self.release_call(call_id)
        self.events.call_state_changed.emit(
            "acc-1",
            call_id,
            "DISCONNECTED",
            487,
            "Request Terminated",
        )


def test_synchronous_disconnected_during_hangup_keeps_result_in_run_complete() -> None:
    events = SipEvents()
    endpoint = SyncDisconnectEndpoint(events)
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)

    completed_results: list[RunnerResult] = []
    completed_runs: list[list[RunnerResult]] = []
    runner.call_completed.connect(completed_results.append)
    runner.run_complete.connect(lambda emitted: completed_runs.append(list(emitted)))
    runner.start()

    call_id = first_call_id(endpoint)
    events.call_state_changed.emit("acc-1", call_id, "EARLY", 180, "Ringing")

    assert len(completed_results) == 1
    assert len(completed_runs) == 1
    assert len(completed_runs[0]) == 1
    assert completed_runs[0][0] is completed_results[0]
    assert completed_runs[0][0].result == "PASS"


class FirstBrokenThenNormalEndpoint(StubEndpoint):
    def __init__(self) -> None:
        super().__init__()
        self.broken_call = BrokenInfoCall()
        self.make_call_count = 0

    def make_call(self, account_id: str, target_uri: str, **_kwargs) -> object:
        self.make_call_count += 1
        if self.make_call_count == 1:
            return self.broken_call
        return super().make_call(account_id, target_uri, **_kwargs)


def test_getinfo_failure_holds_parallel_slot_until_cleanup_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "CLEANUP_FALLBACK_SECONDS", 0.01)
    events = SipEvents()
    endpoint = FirstBrokenThenNormalEndpoint()
    runner = Runner(
        spec(
            callers=["1001", "1001"],
            targets=["2001", "2002"],
            parallel=1,
        ),
        [account()],
        endpoint=endpoint,
        events=events,
    )

    results: list[RunnerResult] = []
    started: list[int] = []
    runner.call_completed.connect(results.append)
    runner.call_started.connect(started.append)
    runner.start()

    assert len(results) == 1
    assert results[0].result == "FAIL"
    assert results[0].sip_reason == "Endpoint error"
    assert results[0].notes == "getInfo failed"
    assert endpoint.hung_up == [endpoint.broken_call]
    assert endpoint.make_call_count == 1
    assert started == []
    assert endpoint.calls == {}

    wait_until(lambda: endpoint.make_call_count == 2, timeout_ms=500)

    assert started == [2]
    assert len(endpoint.calls) == 1
    second_id = first_call_id(endpoint)
    emit_state(events, endpoint, second_id, "EARLY", 180, "Ringing")
    emit_state(events, endpoint, second_id, "DISCONNECTED", 487, "Request Terminated")
    assert len(results) == 2
    assert results[1].result == "PASS"


# ---------------------------------------------------------------------------
# Per-target serialization: never two simultaneous calls to the same URI.
# Was previously broken — `times=N` on one number fired N concurrent INVITEs
# to the same destination, calls 2..N got 486 Busy / 408 from the carrier
# and produced meaningless FAS verdicts on those duplicates.
# ---------------------------------------------------------------------------


def test_single_target_times_n_runs_strictly_sequentially() -> None:
    """5 calls to the same number, parallel=10 — only one in flight at a
    time. With the old grouped queue + no collision check, all 5 INVITEs
    would have fired concurrently."""
    events = SipEvents()
    endpoint = StubEndpoint()
    spec_ = RunnerSpec(
        callers=["1001"],
        targets=["2001"],
        mode="paired",
        pass_criterion="reachability",
        parallel=10,
        hold_seconds=0.01,
        timeout_seconds=0.2,
        times=5,
    )
    runner = Runner(spec_, [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()

    # Drain each call: at every moment, exactly 1 in flight.
    for _ in range(5):
        assert len(endpoint.calls) == 1, (
            f"per-target serialization broken: {len(endpoint.calls)} concurrent "
            "calls to the same target"
        )
        call_id = first_call_id(endpoint)
        emit_state(events, endpoint, call_id, "EARLY", 180, "Ringing")
        emit_state(events, endpoint, call_id, "DISCONNECTED", 487, "Request Terminated")
        # Cleanup timer needs a tick to release the slot and refill.
        wait_until(lambda: call_id not in endpoint.calls, timeout_ms=500)

    wait_for_completed(results, count=5)
    assert len(results) == 5
    assert endpoint.max_active == 1, (
        f"expected max 1 concurrent call to a single target, saw {endpoint.max_active}"
    )


def test_three_distinct_targets_cycle_three_wide_not_ten() -> None:
    """3 distinct targets × times=2 with parallel=10 → at most 3 concurrent
    calls (one per distinct destination URI), dispatched round-robin."""
    events = SipEvents()
    endpoint = StubEndpoint()
    spec_ = RunnerSpec(
        callers=["1001"],
        targets=["2001", "2002", "2003"],
        mode="paired",  # 1 caller × 3 targets promotes to fan-out shape
        pass_criterion="reachability",
        parallel=10,
        hold_seconds=0.01,
        timeout_seconds=0.2,
        times=2,
    )
    runner = Runner(spec_, [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()

    # All 3 distinct targets dispatch immediately (round-robin queue).
    assert len(endpoint.calls) == 3
    target_uris = {tup[1] for tup in endpoint.calls.values()}
    assert target_uris == {
        "sip:2001@pbx.example.test",
        "sip:2002@pbx.example.test",
        "sip:2003@pbx.example.test",
    }, f"expected one in-flight call per distinct target, got {target_uris}"

    # Drain the first cycle; the second cycle (times=2) should kick in.
    for cid in list(endpoint.calls.keys()):
        emit_state(events, endpoint, cid, "EARLY", 180, "Ringing")
        emit_state(events, endpoint, cid, "DISCONNECTED", 487, "Request Terminated")
    wait_until(lambda: len(endpoint.calls) >= 3 or len(results) >= 6, timeout_ms=500)

    # Drain second cycle.
    for cid in list(endpoint.calls.keys()):
        emit_state(events, endpoint, cid, "EARLY", 180, "Ringing")
        emit_state(events, endpoint, cid, "DISCONNECTED", 487, "Request Terminated")
    wait_for_completed(results, count=6)

    assert len(results) == 6
    assert endpoint.max_active == 3, (
        f"expected max 3 concurrent calls (one per distinct target), saw {endpoint.max_active}"
    )


def test_numeric_caller_is_an_origination_a_number() -> None:
    """ORIGINATION numbers in Callers dial from the active account and are
    presented as the call's A-number (they used to fail "No matching
    account" without a single INVITE)."""
    seen: list[dict] = []

    class _Recording(StubEndpoint):
        def make_call(self, account_id, target_uri, **kwargs):
            seen.append({"account": account_id, **kwargs})
            return super().make_call(account_id, target_uri, **kwargs)

    events = SipEvents()
    endpoint = _Recording()
    acc = account()
    runner = Runner(spec(callers=["+20 100-123-4567"]), [acc], endpoint=endpoint, events=events)
    runner.start()
    assert seen and seen[0]["account"] == acc.id
    assert seen[0]["a_number"] == "+201001234567"


def test_account_username_caller_keeps_account_identity() -> None:
    seen: list[dict] = []

    class _Recording(StubEndpoint):
        def make_call(self, account_id, target_uri, **kwargs):
            seen.append(kwargs)
            return super().make_call(account_id, target_uri, **kwargs)

    acc = account()
    runner = Runner(spec(callers=[acc.username]), [acc], endpoint=_Recording(), events=SipEvents())
    runner.start()
    assert seen and seen[0]["a_number"] == ""


# ---------------------------------------------------------------------------
# 2026-09 runner fixes (each reproduced on the native engine first; see
# tools/runner_loopback_check.py)
# ---------------------------------------------------------------------------
def test_target_uri_carries_the_account_port() -> None:
    """A bare number on an account with a custom port went to domain:5060."""
    acc = account()
    acc.port = 5080
    runner = Runner(spec(), [acc], endpoint=StubEndpoint(), events=SipEvents())
    assert runner._build_target_uri("2001", acc) == "sip:2001@pbx.example.test:5080"
    acc.port = 0
    assert runner._build_target_uri("2001", acc) == "sip:2001@pbx.example.test"


class _SlotError(Exception):
    status = 70010  # PJ_ETOOMANY

    def __str__(self) -> str:
        return "Too many objects of the specified type (PJ_ETOOMANY)"


def test_no_free_call_slot_requeues_instead_of_recording_a_fake_fail(monkeypatch) -> None:
    monkeypatch.setattr(runner_module, "SLOT_WAIT_SECONDS", 0.02)

    class _BusyThenFree(StubEndpoint):
        def __init__(self) -> None:
            super().__init__()
            self.refusals = 3

        def make_call(self, account_id, target_uri, **kwargs):
            if self.refusals:
                self.refusals -= 1
                raise _SlotError()
            return super().make_call(account_id, target_uri, **kwargs)

    events = SipEvents()
    endpoint = _BusyThenFree()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    wait_until(lambda: bool(endpoint.calls), timeout_ms=2000)
    assert results == []                    # no fake FAIL for the refusals
    emit_state(events, endpoint, first_call_id(endpoint), "EARLY", 180, "Ringing")
    wait_until(lambda: len(results) == 1)
    assert results[0].result == "PASS"


def test_slot_wait_gives_up_as_error_not_supplier_fail(monkeypatch) -> None:
    monkeypatch.setattr(runner_module, "SLOT_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(runner_module, "SLOT_WAIT_MAX_TRIES", 2)

    class _NeverFree(StubEndpoint):
        def make_call(self, account_id, target_uri, **kwargs):
            raise _SlotError()

    runner = Runner(spec(), [account()], endpoint=_NeverFree(), events=SipEvents())
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    wait_until(lambda: len(results) == 1, timeout_ms=2000)
    assert results[0].result == "ERROR" and results[0].sip_reason == "No free call slot"


def test_fas_sweep_waits_the_jitter_before_reprobing_a_target() -> None:
    dialled_at: list[float] = []

    class _Timed(StubEndpoint):
        def make_call(self, account_id, target_uri, **kwargs):
            dialled_at.append(time.monotonic())
            return super().make_call(account_id, target_uri, **kwargs)

    events = SipEvents()
    endpoint = _Timed()
    s = RunnerSpec(callers=["1001"], targets=["2001"], mode="fas-sweep",
                   pass_criterion="reachability", parallel=4, hold_seconds=0.01,
                   timeout_seconds=5, tries_per_pair=2, jitter_low_s=0.3, jitter_high_s=0.3)
    runner = Runner(s, [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    first = first_call_id(endpoint)
    emit_state(events, endpoint, first, "DISCONNECTED", 486, "Busy Here")
    wait_until(lambda: len(results) == 1)
    completed_at = time.monotonic()
    wait_until(lambda: len(dialled_at) == 2, timeout_ms=2000)
    assert dialled_at[1] - completed_at >= 0.25   # cooled off, not back-to-back


def test_runner_does_not_keep_finished_calls_alive() -> None:
    """The runner must never own a pjsua2 Call: releasing one after PJSIP
    reused its slot hangs up the call now in that slot."""
    import gc
    import weakref

    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    runner.start()
    cid = first_call_id(endpoint)
    probe = weakref.ref(endpoint.calls[cid][2])
    emit_state(events, endpoint, cid, "DISCONNECTED", 486, "Busy Here")
    wait_until(lambda: runner._run_complete_emitted, timeout_ms=2000)
    endpoint.calls.clear()   # the account drops it on DISCONNECTED
    gc.collect()
    assert probe() is None
