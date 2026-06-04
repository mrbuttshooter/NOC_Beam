"""Production-mirror investigation of TestRunner failure modes.

User reported "in real life most failed" — this file exhaustively
tests every observed and plausible failure scenario in batch test
runs against wholesale SIP carriers.

Scenarios covered:
- Real carrier rejections (480, 486, 487, 403, 404, 503, 408, 500, 484, 488)
- Auth challenge dance (401 then success, 401 then 403, 407 then 502)
- Local errors (make_call raises, pjsua2 unavailable, unknown account,
  PJSIP_EINVALIDURI, no matching account)
- Lifecycle edge cases (unknown call_id, wrong account_id, peer BYE during
  hold, multiple CONFIRMED)
- Parallel batch with mixed outcomes
- Routing edge cases (SIP URI passthrough, user@host scheme prepend,
  dial_prefix double-prepend guard, Genband supplier prefix)
- Concurrency (cancel during auth retry, timeout during auth retry,
  late-arriving DISCONNECTED after run_complete)
- Result-field invariants

Uses StubEndpoint (no real PJSIP). Mirrors real-world batch behavior.
"""
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

    def getInfo(self):
        return self._info


class StubEndpoint:
    def __init__(self) -> None:
        self.next_call_id = 100
        self.calls: dict[int, tuple[str, str, object]] = {}
        self.hung_up: list[object] = []
        self.make_call_error: Exception | None = None
        self.dispatched: list[tuple[str, str]] = []

    def make_call(self, account_id: str, target_uri: str, **_kwargs):
        self.dispatched.append((account_id, target_uri))
        if self.make_call_error is not None:
            raise self.make_call_error
        call = StubCall(self.next_call_id)
        self.next_call_id += 1
        self.calls[call.getInfo().id] = (account_id, target_uri, call)
        return call

    def hangup_call(self, call: object) -> None:
        self.hung_up.append(call)


def account(username: str = "1001", account_id: str = "acc-1", **extra) -> AccountConfig:
    return AccountConfig(
        id=account_id,
        username=username,
        domain="pbx.example.test",
        **extra,
    )


def spec(
    *,
    callers: list[str] | None = None,
    targets: list[str] | None = None,
    pass_criterion: str = "reachability",
    parallel: int = 1,
    hold_seconds: float = 0.01,
    timeout_seconds: float = 0.3,
) -> RunnerSpec:
    return RunnerSpec(
        callers=callers or ["1001"],
        targets=targets or ["2001"],
        mode="paired",
        pass_criterion=pass_criterion,
        parallel=parallel,
        hold_seconds=hold_seconds,
        timeout_seconds=timeout_seconds,
    )


def wait_until(predicate: Callable[[], bool], timeout_ms: int = 1500) -> None:
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
        endpoint.calls.pop(call_id, None)
    events.call_state_changed.emit(account_id, call_id, state, code, reason)


def wait_for_completed(results: list[RunnerResult], count: int = 1) -> None:
    wait_until(lambda: len(results) >= count, timeout_ms=1500)


# ---------- Carrier rejections ----------
@pytest.mark.parametrize("code,reason", [
    (480, "Temporarily Unavailable"),
    (486, "Busy Here"),
    (487, "Request Terminated"),
    (403, "Forbidden"),
    (404, "Not Found"),
    (503, "Service Unavailable"),
    (408, "Request Timeout"),
    (500, "Server Internal Error"),
    (484, "Address Incomplete"),
    (488, "Not Acceptable Here"),
])
def test_carrier_rejection_codes_produce_FAIL_with_real_reason(code: int, reason: str) -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, call_id, "DISCONNECTED", code, reason)
    wait_for_completed(results)
    assert results[0].result == "FAIL"
    assert results[0].sip_code == code
    assert reason in results[0].sip_reason


# ---------- Auth dance ----------
def test_401_then_200_OK_passes_full_call_after_auth_retry() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(pass_criterion="full-call", hold_seconds=0.02),
                    [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, call_id, "CALLING", 401, "Unauthorized")
    assert results == []
    emit_state(events, endpoint, call_id, "CONFIRMED", 200, "OK")
    wait_for_completed(results)
    assert results[0].result == "PASS"
    assert results[0].sip_code == 200


def test_407_then_502_proxy_auth_then_bad_gateway_fails_with_502() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, call_id, "CALLING", 407, "Proxy Authentication Required")
    assert results == []
    emit_state(events, endpoint, call_id, "DISCONNECTED", 502, "Bad Gateway")
    wait_for_completed(results)
    assert results[0].result == "FAIL"
    assert results[0].sip_code == 502


def test_repeated_401_then_real_403_rejection_fails_once_with_403() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, call_id, "CALLING", 401, "Unauthorized")
    emit_state(events, endpoint, call_id, "DISCONNECTED", 403, "Forbidden")
    wait_for_completed(results)
    assert len(results) == 1
    assert results[0].sip_code == 403


# ---------- Local errors ----------
def test_endpoint_make_call_raises_pjsua2_unavailable_is_normalized() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    endpoint.make_call_error = RuntimeError("pjsua2 not available -- install the wheel")
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    wait_for_completed(results)
    assert results[0].result == "FAIL"
    assert results[0].notes == "pjsua2 not available"


def test_endpoint_make_call_raises_unknown_account_uuid_surfaces_full_text() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    endpoint.make_call_error = ValueError("Unknown account 471b157e-6ff3-42e5-93ce-7b6a193cd5a4")
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    wait_for_completed(results)
    assert results[0].result == "FAIL"
    assert "Unknown account" in results[0].notes


def test_endpoint_pjsip_invalidurI_surfaces_in_notes() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    endpoint.make_call_error = RuntimeError(
        "PJSIP rejected the account: Invalid URI (PJSIP_EINVALIDURI) (status=171039)"
    )
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    wait_for_completed(results)
    assert results[0].result == "FAIL"
    assert "PJSIP_EINVALIDURI" in results[0].notes


def test_no_matching_account_for_caller_number_does_not_dispatch() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    s = spec(callers=["9999"], targets=["2001"])
    runner = Runner(s, [account(username="1001")], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    wait_for_completed(results)
    assert endpoint.dispatched == []
    assert results[0].result == "FAIL"


# ---------- Lifecycle edge cases ----------
def test_unknown_call_id_signal_is_silently_ignored() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, 99999, "DISCONNECTED", 500, "Server Internal")
    assert results == []
    emit_state(events, endpoint, call_id, "EARLY", 180, "Ringing")
    wait_for_completed(results)
    assert results[0].sip_code == 180


def test_signal_for_wrong_account_id_is_ignored() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, call_id, "DISCONNECTED", 480, "Temp",
               account_id="wrong-account")
    assert results == []


def test_confirmed_then_peer_BYE_during_hold_timer_produces_one_result() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(pass_criterion="full-call", hold_seconds=0.5),
                    [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, call_id, "CONFIRMED", 200, "OK")
    emit_state(events, endpoint, call_id, "DISCONNECTED", 200, "Normal Clearing")
    wait_for_completed(results)
    assert len(results) == 1


def test_multiple_confirmed_signals_only_emit_one_result() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(pass_criterion="full-call", hold_seconds=0.02),
                    [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, call_id, "CONFIRMED", 200, "OK")
    emit_state(events, endpoint, call_id, "CONFIRMED", 200, "OK")
    emit_state(events, endpoint, call_id, "CONFIRMED", 200, "OK")
    wait_for_completed(results)
    assert len(results) == 1
    assert results[0].result == "PASS"


# ---------- Parallel batch ----------
def test_parallel_batch_with_mixed_real_carrier_responses() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(
        spec(callers=["1001"], targets=["2001", "2002", "2003", "2004"], parallel=4),
        [account()], endpoint=endpoint, events=events,
    )
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    wait_until(lambda: len(endpoint.calls) >= 4, timeout_ms=500)
    ids = list(endpoint.calls.keys())
    emit_state(events, endpoint, ids[0], "EARLY", 180, "Ringing")
    emit_state(events, endpoint, ids[1], "DISCONNECTED", 480, "Subscriber Absent")
    emit_state(events, endpoint, ids[2], "DISCONNECTED", 486, "Busy Here")
    emit_state(events, endpoint, ids[3], "DISCONNECTED", 503, "Service Unavailable")
    wait_for_completed(results, count=4)
    codes = sorted(r.sip_code for r in results if r.sip_code is not None)
    assert codes == [180, 480, 486, 503]
    assert sum(1 for r in results if r.result == "PASS") == 1
    assert sum(1 for r in results if r.result == "FAIL") == 3


def test_all_calls_in_batch_fail_with_same_code_drains_queue_cleanly() -> None:
    """User's production scenario: 8 calls, every one gets 480. Queue
    must fully drain (no slot starvation when nothing PASSes)."""
    targets = [f"2{n:03d}" for n in range(8)]
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(callers=["1001"], targets=targets, parallel=3),
                    [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    completed: list[object] = []
    runner.call_completed.connect(results.append)
    runner.run_complete.connect(completed.append)
    runner.start()
    deadline = time.monotonic() + 3.0
    while len(results) < len(targets) and time.monotonic() < deadline:
        if endpoint.calls:
            cid = next(iter(endpoint.calls))
            emit_state(events, endpoint, cid, "DISCONNECTED", 480, "Subscriber Absent")
        wait_until(lambda: True, timeout_ms=20)
    wait_for_completed(results, count=len(targets))
    wait_until(lambda: len(completed) > 0, timeout_ms=2000)
    assert len(results) == len(targets)
    assert all(r.sip_code == 480 for r in results)
    assert len(completed) == 1


# ---------- Routing edge cases ----------
def test_target_already_a_sip_uri_is_passed_through_unchanged() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    s = spec(callers=["1001"], targets=["sip:music@iptel.org"])
    runner = Runner(s, [account()], endpoint=endpoint, events=events)
    runner.start()
    wait_until(lambda: bool(endpoint.dispatched), timeout_ms=500)
    assert endpoint.dispatched[0][1] == "sip:music@iptel.org"


def test_target_with_at_sign_gets_sip_scheme_prepended() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    s = spec(callers=["1001"], targets=["echo@iptel.org"])
    runner = Runner(s, [account()], endpoint=endpoint, events=events)
    runner.start()
    wait_until(lambda: bool(endpoint.dispatched), timeout_ms=500)
    assert endpoint.dispatched[0][1] == "sip:echo@iptel.org"


def test_target_with_dial_prefix_already_set_does_not_double_prepend() -> None:
    acc = account(username="1001")
    acc.dial_prefix = "00"
    events = SipEvents()
    endpoint = StubEndpoint()
    s = spec(callers=["1001"], targets=["0096171488860"])
    runner = Runner(s, [acc], endpoint=endpoint, events=events)
    runner.start()
    wait_until(lambda: bool(endpoint.dispatched), timeout_ms=500)
    target = endpoint.dispatched[0][1]
    user = target.split(":", 1)[1].split("@", 1)[0]
    assert user == "0096171488860"


def test_genband_supplier_prefix_prepended_for_genband_switch_type() -> None:
    acc = account(username="1001")
    acc.switch_type = "genband"
    acc.routing_format = "000{id}"
    events = SipEvents()
    endpoint = StubEndpoint()
    s = spec(callers=["1001"], targets=["6171488860"])
    runner = Runner(s, [acc], endpoint=endpoint, events=events, supplier_id="080")
    runner.start()
    wait_until(lambda: bool(endpoint.dispatched), timeout_ms=500)
    target = endpoint.dispatched[0][1]
    user = target.split(":", 1)[1].split("@", 1)[0]
    assert user.startswith("000080")
    assert user.endswith("6171488860")


# ---------- Concurrency ----------
def test_cancel_while_calls_are_in_auth_retry_does_not_double_complete() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(callers=["1001"], targets=["2001", "2002"], parallel=2),
                    [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    wait_until(lambda: len(endpoint.calls) >= 2, timeout_ms=500)
    ids = list(endpoint.calls.keys())
    emit_state(events, endpoint, ids[0], "CALLING", 401, "Unauthorized")
    assert results == []
    runner.cancel()
    wait_for_completed(results, count=2)
    assert len(results) == 2
    assert all(r.result == "FAIL" for r in results)
    assert all("cancel" in r.notes.lower() for r in results)


def test_timeout_fires_while_call_is_mid_auth_retry() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(timeout_seconds=0.1),
                    [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, call_id, "CALLING", 401, "Unauthorized")
    assert results == []
    wait_for_completed(results, count=1)
    assert results[0].sip_code == 408
    assert "timeout" in results[0].notes.lower()


def test_run_complete_emits_once_even_with_late_arriving_disconnect() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    completed_count = [0]
    runner.run_complete.connect(lambda _r: completed_count.__setitem__(0, completed_count[0] + 1))
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, call_id, "EARLY", 180, "Ringing")
    emit_state(events, endpoint, call_id, "DISCONNECTED", 487, "Request Terminated")
    wait_until(lambda: completed_count[0] >= 1, timeout_ms=1000)
    emit_state(events, endpoint, call_id, "DISCONNECTED", 487, "late")
    wait_until(lambda: True, timeout_ms=200)
    assert completed_count[0] == 1


# ---------- Result-correctness invariants ----------
def test_every_result_has_consistent_required_fields() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    s = spec(callers=["1001"], targets=["t1", "t2", "t3"], parallel=3, timeout_seconds=0.1)
    runner = Runner(s, [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    wait_until(lambda: len(endpoint.calls) >= 3, timeout_ms=500)
    ids = list(endpoint.calls.keys())
    emit_state(events, endpoint, ids[0], "EARLY", 180, "Ringing")
    emit_state(events, endpoint, ids[1], "DISCONNECTED", 480, "Temp")
    wait_for_completed(results, count=3)
    for r in results:
        assert r.result in ("PASS", "FAIL")
        assert isinstance(r.sip_reason, str) and r.sip_reason
        assert r.duration_s >= 0.0
        assert r.started_at > 0.0
        assert r.from_account == "acc-1"
        assert r.to_uri


# ---------- Transport / local PJSIP errors (not supplier rejects) ----------
@pytest.mark.parametrize("code,reason", [
    (503, "End of file (PJ_EEOF)"),            # shared TCP connection closed
    (503, "(PJSIP_ETRANSPORTDISCONNECTED)"),
    (0, "Timeout (PJ_ETIMEDOUT)"),
])
def test_transport_errors_classified_ERROR_not_supplier_fail(code: int, reason: str) -> None:
    """PJ_EEOF and friends are the local TCP connection dying, not a
    supplier reject. They must be ERROR (kept out of the FAIL/route
    success rate), even though pjsua2 surfaces them as a synthetic 503."""
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, call_id, "DISCONNECTED", code, reason)
    wait_for_completed(results)
    assert results[0].result == "ERROR"
    assert reason in results[0].sip_reason


def test_real_503_congestion_stays_FAIL_not_error() -> None:
    """A genuine 503 from the supplier (no PJ_E token in the reason) is a
    route FAIL with a congestion note — NOT reclassified as ERROR."""
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)
    results: list[RunnerResult] = []
    runner.call_completed.connect(results.append)
    runner.start()
    call_id = first_call_id(endpoint)
    emit_state(events, endpoint, call_id, "DISCONNECTED", 503, "Service Unavailable")
    wait_for_completed(results)
    assert results[0].result == "FAIL"
    assert results[0].sip_code == 503
    assert "congestion" in results[0].notes.lower()


def test_max_cps_paces_dispatch_of_distinct_targets() -> None:
    """With max_cps set and several distinct targets, the runner must not
    fire them all in the same instant — consecutive dials are spaced by
    >= 1/max_cps. Without pacing all 3 dispatch synchronously at start()."""
    events = SipEvents()
    endpoint = StubEndpoint()
    spec_ = RunnerSpec(
        callers=["1001"],
        targets=["2001", "2002", "2003"],
        mode="paired",
        pass_criterion="reachability",
        parallel=10,
        hold_seconds=0.01,
        timeout_seconds=0.3,
        max_cps=20.0,  # 50 ms minimum gap between dials
    )
    runner = Runner(spec_, [account()], endpoint=endpoint, events=events)
    runner.start()
    # First dial fires synchronously inside start(); the next two are
    # gated behind the pacing timer, so they have NOT all landed yet.
    assert len(endpoint.dispatched) == 1, (
        f"pacing failed: {len(endpoint.dispatched)} dials fired in the same instant"
    )
    # Let the pacing timers drain; eventually all 3 distinct targets dial.
    wait_until(lambda: len(endpoint.dispatched) >= 3, timeout_ms=2000)
    assert len(endpoint.dispatched) >= 3
