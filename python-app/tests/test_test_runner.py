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


class StubEndpoint:
    def __init__(self) -> None:
        self.next_call_id = 100
        self.calls: dict[int, tuple[str, str, StubCall]] = {}
        self.hung_up: list[int] = []
        self.max_active = 0

    def make_call(self, account_id: str, target_uri: str) -> StubCall:
        call = StubCall(self.next_call_id)
        self.next_call_id += 1
        self.calls[call.getInfo().id] = (account_id, target_uri, call)
        self.max_active = max(self.max_active, len(self.calls))
        return call

    def hangup_call(self, call: StubCall) -> None:
        call_id = call.getInfo().id
        self.hung_up.append(call_id)
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


def test_reachability_passes_on_first_180_ringing() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)

    results: list[RunnerResult] = []
    runner.run_complete.connect(lambda emitted: results.extend(emitted))
    runner.start()

    call_id = first_call_id(endpoint)
    events.call_state_changed.emit("acc-1", call_id, "EARLY", 180, "Ringing")

    assert len(results) == 1
    assert results[0].result == "PASS"
    assert results[0].sip_code == 180
    assert results[0].sip_reason == "Ringing"
    assert results[0].to_uri == "sip:2001@pbx.example.test"
    assert endpoint.hung_up == [call_id]


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
    runner.run_complete.connect(lambda emitted: results.extend(emitted))
    runner.start()

    call_id = first_call_id(endpoint)
    events.call_state_changed.emit("acc-1", call_id, "CONFIRMED", 200, "OK")
    wait_until(lambda: bool(results), timeout_ms=500)

    assert results[0].result == "PASS"
    assert results[0].sip_code == 200
    assert results[0].sip_reason == "OK"
    assert endpoint.hung_up == [call_id]


def test_fails_on_404() -> None:
    events = SipEvents()
    endpoint = StubEndpoint()
    runner = Runner(spec(), [account()], endpoint=endpoint, events=events)

    results: list[RunnerResult] = []
    runner.run_complete.connect(lambda emitted: results.extend(emitted))
    runner.start()

    call_id = first_call_id(endpoint)
    events.call_state_changed.emit("acc-1", call_id, "DISCONNECTED", 404, "Not Found")

    assert len(results) == 1
    assert results[0].result == "FAIL"
    assert results[0].sip_code == 404
    assert results[0].sip_reason == "Not Found"
    assert endpoint.hung_up == [call_id]


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
    runner.run_complete.connect(lambda emitted: results.extend(emitted))
    runner.start()

    call_id = first_call_id(endpoint)
    wait_until(lambda: bool(results), timeout_ms=500)

    assert results[0].result == "FAIL"
    assert results[0].sip_code == 408
    assert results[0].sip_reason == "Request Timeout"
    assert results[0].notes == "timeout"
    assert endpoint.hung_up == [call_id]


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
        events.call_state_changed.emit("acc-1", call_id, "EARLY", 180, "Ringing")

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
    runner.cancel()

    assert len(results) == 4
    assert all(result.result == "FAIL" for result in results)
    assert all(result.sip_reason == "Cancelled" for result in results)
    assert all(result.notes == "cancelled" for result in results)
    assert endpoint.hung_up == active_call_ids
    assert endpoint.calls == {}
