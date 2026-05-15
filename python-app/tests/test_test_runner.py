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

    def make_call(self, account_id: str, target_uri: str) -> StubCall:
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
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(
        spec(callers=["9999"]),
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

    def make_call(self, account_id: str, target_uri: str) -> BrokenInfoCall:
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

    def make_call(self, account_id: str, target_uri: str) -> object:
        self.make_call_count += 1
        if self.make_call_count == 1:
            return self.broken_call
        return super().make_call(account_id, target_uri)


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
