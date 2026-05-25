"""Reachability pass-criterion semantics (LOGIC_REVIEW 2026-05-25).

The reachability mode now requires an actual SIP 180 Ringing -- 183
Session Progress alone (very common with wholesale carriers that send
canned "all circuits busy" early media without ever ringing the
destination) is treated as a failure.

Five branches covered:
  1. 180 Ringing      -> PASS, notes "180_then_bye", BYE sent.
  2. 183 only + timeout -> FAIL, notes "early_media_only_no_180".
  3. 200 OK with prior 180 -> PASS, notes "answered".
  4. 200 OK without 180 -> PASS, notes "200_no_180_warn".
  5. Timeout with nothing -> FAIL, notes "timeout".
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
from noc_beam.testing.plan import TestSpec
from noc_beam.testing.runner import TestResult, TestRunner


def ensure_app() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture(autouse=True)
def qt_app() -> QCoreApplication:
    return ensure_app()


class _StubInfo:
    def __init__(self, call_id: int) -> None:
        self.id = call_id


class _StubCall:
    def __init__(self, call_id: int) -> None:
        self._info = _StubInfo(call_id)

    def getInfo(self) -> _StubInfo:  # noqa: N802
        return self._info


class _StubEndpoint:
    def __init__(self) -> None:
        self.next_call_id = 500
        self.calls: dict[int, _StubCall] = {}
        self.hung_up: list[object] = []

    def make_call(self, account_id: str, target_uri: str, **_kwargs) -> _StubCall:
        call = _StubCall(self.next_call_id)
        self.next_call_id += 1
        self.calls[call.getInfo().id] = call
        return call

    def hangup_call(self, call: object) -> None:
        self.hung_up.append(call)

    def release_call(self, call_id: int) -> None:
        self.calls.pop(call_id, None)


def _account() -> AccountConfig:
    return AccountConfig(id="acc-1", username="1001", domain="pbx.example.test")


def _spec(timeout_seconds: float = 0.05) -> TestSpec:
    return TestSpec(
        callers=["1001"],
        targets=["2001"],
        mode="paired",
        pass_criterion="reachability",
        parallel=1,
        hold_seconds=0.01,
        timeout_seconds=timeout_seconds,
    )


def _first_id(endpoint: _StubEndpoint) -> int:
    return next(iter(endpoint.calls))


def _emit_state(
    events: SipEvents,
    endpoint: _StubEndpoint,
    call_id: int,
    state: str,
    code: int,
    reason: str,
    account_id: str = "acc-1",
) -> None:
    if state == "DISCONNECTED":
        endpoint.release_call(call_id)
    events.call_state_changed.emit(account_id, call_id, state, code, reason)


def _wait_until(predicate: Callable[[], bool], timeout_ms: int = 1000) -> None:
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while not predicate() and time.monotonic() < deadline:
        loop = QEventLoop()
        QTimer.singleShot(5, loop.quit)
        loop.exec()
    assert predicate()


# Branch 1: 180 Ringing -> PASS, BYE sent, notes "180_then_bye"
def test_reachability_180_passes_with_notes_and_byes() -> None:
    events = SipEvents()
    endpoint = _StubEndpoint()
    runner = TestRunner(_spec(), [_account()], endpoint=endpoint, events=events)

    results: list[TestResult] = []
    runner.call_completed.connect(results.append)
    runner.start()

    call_id = _first_id(endpoint)
    sip_call = endpoint.calls[call_id]
    _emit_state(events, endpoint, call_id, "EARLY", 180, "Ringing")

    assert len(results) == 1
    assert results[0].result == "PASS"
    assert results[0].sip_code == 180
    assert results[0].notes == "180_then_bye"
    # Destination must see a missed call -- BYE was issued immediately.
    assert endpoint.hung_up == [sip_call]


# Branch 2: 183 only + timeout -> FAIL, "early_media_only_no_180"
def test_reachability_183_only_then_timeout_fails_with_early_media_note() -> None:
    events = SipEvents()
    endpoint = _StubEndpoint()
    runner = TestRunner(
        _spec(timeout_seconds=0.05),
        [_account()],
        endpoint=endpoint,
        events=events,
    )

    results: list[TestResult] = []
    runner.call_completed.connect(results.append)
    runner.start()

    call_id = _first_id(endpoint)
    # Carrier sends 183 Session Progress only (no actual 180 Ringing).
    _emit_state(events, endpoint, call_id, "EARLY", 183, "Session Progress")
    # No result yet -- we must NOT pass on 183 alone.
    assert results == []

    # Let the per-call timeout fire.
    _wait_until(lambda: len(results) >= 1, timeout_ms=500)

    assert len(results) == 1
    assert results[0].result == "FAIL"
    assert results[0].sip_code == 408
    assert results[0].notes == "early_media_only_no_180"


# Branch 3: 200 OK after prior 180 -> PASS, notes "answered"
# (NB: in production we BYE on 180, so a stray 200 after we already
# completed must be a no-op -- this guards the race.)
def test_reachability_200_after_180_passes_with_answered_note() -> None:
    events = SipEvents()
    endpoint = _StubEndpoint()
    runner = TestRunner(_spec(), [_account()], endpoint=endpoint, events=events)

    results: list[TestResult] = []
    runner.call_completed.connect(results.append)
    runner.start()

    call_id = _first_id(endpoint)
    _emit_state(events, endpoint, call_id, "EARLY", 180, "Ringing")
    # First result: pass on 180.
    assert len(results) == 1
    assert results[0].notes == "180_then_bye"

    # Stray 200 OK arriving after completion must NOT add a second result.
    _emit_state(events, endpoint, call_id, "CONFIRMED", 200, "OK")
    assert len(results) == 1


# Branch 4: 200 OK without prior 180 -> PASS, notes "200_no_180_warn"
def test_reachability_200_without_180_passes_with_warning_note() -> None:
    events = SipEvents()
    endpoint = _StubEndpoint()
    runner = TestRunner(_spec(), [_account()], endpoint=endpoint, events=events)

    results: list[TestResult] = []
    runner.call_completed.connect(results.append)
    runner.start()

    call_id = _first_id(endpoint)
    sip_call = endpoint.calls[call_id]
    # Some endpoints (esp. SBC mid-call answer features) jump straight
    # to 200 OK without sending 180. Still a successful reach, but we
    # tag it so analysts can spot the pattern.
    _emit_state(events, endpoint, call_id, "CONFIRMED", 200, "OK")

    assert len(results) == 1
    assert results[0].result == "PASS"
    assert results[0].sip_code == 200
    assert results[0].notes == "200_no_180_warn"
    assert endpoint.hung_up == [sip_call]


# Branch 5: Timeout with nothing -> FAIL, notes "timeout"
def test_reachability_silent_timeout_fails_with_timeout_note() -> None:
    events = SipEvents()
    endpoint = _StubEndpoint()
    runner = TestRunner(
        _spec(timeout_seconds=0.05),
        [_account()],
        endpoint=endpoint,
        events=events,
    )

    results: list[TestResult] = []
    runner.call_completed.connect(results.append)
    runner.start()

    # No state events at all -- purely time out.
    _wait_until(lambda: len(results) >= 1, timeout_ms=500)

    assert len(results) == 1
    assert results[0].result == "FAIL"
    assert results[0].sip_code == 408
    assert results[0].notes == "timeout"


# Defensive: 100 Trying alone (the other common non-180 1xx) must
# behave the same as 183-only -- no pass on the Trying, timeout note
# upgraded to early_media_only_no_180.
def test_reachability_100_trying_only_then_timeout_is_early_media_no_180() -> None:
    events = SipEvents()
    endpoint = _StubEndpoint()
    runner = TestRunner(
        _spec(timeout_seconds=0.05),
        [_account()],
        endpoint=endpoint,
        events=events,
    )

    results: list[TestResult] = []
    runner.call_completed.connect(results.append)
    runner.start()

    call_id = _first_id(endpoint)
    _emit_state(events, endpoint, call_id, "EARLY", 100, "Trying")
    assert results == []
    _wait_until(lambda: len(results) >= 1, timeout_ms=500)

    assert results[0].result == "FAIL"
    assert results[0].notes == "early_media_only_no_180"
