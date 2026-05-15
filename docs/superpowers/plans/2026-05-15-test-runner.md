# Test Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a multi-call SIP test runner that expands caller/target paste boxes into queued calls, runs them with bounded parallelism, and exports pass/fail results as CSV.

**Architecture:** Keep pure call-plan expansion in `noc_beam/testing/plan.py`, SIP orchestration in `noc_beam/testing/runner.py`, and Qt presentation/export in `noc_beam/ui/test_runner_view.py`. Runner logic reuses `SipEndpoint` and `sip_events()` with dependency injection for tests, so the UI path and stubbed unit tests exercise the same state machine.

**Tech Stack:** Python 3.11+, PySide6 Qt signals/timers/widgets, pytest, existing `AccountConfig`, existing `SipEndpoint`, existing `SipEvents`.

---

## File Structure

- Create `python-app/src/noc_beam/testing/__init__.py`
  - Marks `noc_beam.testing` as an importable package.
- Create `python-app/src/noc_beam/testing/plan.py`
  - Owns `TestSpec`, `TestCall`, `normalise_lines()`, and `expand()`.
  - Has no Qt, no SIP imports, and no side effects.
- Create `python-app/src/noc_beam/testing/runner.py`
  - Owns `TestResult`, internal active-call tracking, and `TestRunner(QObject)`.
  - Uses `SipEndpoint.instance()` and `sip_events()` by default, but accepts injected `endpoint` and `events` keyword-only arguments for tests.
- Create `python-app/src/noc_beam/ui/test_runner_view.py`
  - Owns the window, paste boxes, controls, result grid, footer counters, cancel, and CSV export.
  - Does not duplicate matrix expansion or SIP state logic.
- Modify `python-app/src/noc_beam/ui/phone_shell.py`
  - Adds `"Test Runner..."` to the existing View group.
  - Adds `_on_open_test_runner()` using the same lazy window pattern as accounts/diagnostics.
- Create `python-app/tests/test_test_plan.py`
  - Covers all modes, whitespace handling, duplicates, empty input, and parallel clamping.
- Create `python-app/tests/test_test_runner.py`
  - Uses a stub endpoint and fresh `SipEvents()` object.
  - Covers reachability pass, full-call pass, SIP failure, no account, timeout, concurrency, and cancel.
- Create `python-app/tests/test_test_runner_view.py`
  - Uses offscreen Qt.
  - Covers construction, disabled empty run button, live count updates, hold enablement, and CSV export content.

## Implementation Notes

- Use exact mode values from the spec: `"matrix"`, `"paired"`, `"fan-out"`, `"fan-in"`.
- Use exact pass criteria from the spec: `"reachability"`, `"full-call"`.
- Cap `parallel` in `TestSpec.__post_init__()` to `1..16` so both direct code and UI spinner respect `PJSUA_MAX_CALLS`.
- Keep duplicate caller/target lines because batch retests of the same route are valid.
- Build target URIs in the runner as `sip:<target>@<account.domain>` unless target already starts with `sip:`, `sips:`, `tel:`, or contains `@`.
- Treat `EARLY` with status `180` or `183` as reachability success.
- Treat any SIP code `400..699` as failure unless that call already reached its success state.
- In stub mode, catch `SipEndpoint.make_call()` exceptions per call and emit `FAIL`, `sip_code=0`, and `notes="pjsua2 not available"` when the exception text contains that phrase. Otherwise use the exception text as `notes`.
- The runner should not register calls with `CallManager`; the existing app-level call manager already listens to SIP events for the live softphone. The test runner only tracks its own call ids so it does not create duplicate UI call records.

---

### Task 1: Pure Test Plan Expansion

**Files:**
- Create: `python-app/src/noc_beam/testing/__init__.py`
- Create: `python-app/src/noc_beam/testing/plan.py`
- Test: `python-app/tests/test_test_plan.py`

- [ ] **Step 1: Write failing plan tests**

Create `python-app/tests/test_test_plan.py`:

```python
from __future__ import annotations

from noc_beam.testing.plan import TestSpec, expand, normalise_lines


def spec(callers: list[str], targets: list[str], mode: str) -> TestSpec:
    return TestSpec(
        callers=callers,
        targets=targets,
        mode=mode,
        pass_criterion="reachability",
        parallel=4,
        hold_seconds=2.0,
        timeout_seconds=30.0,
    )


def pairs(calls):
    return [(c.index, c.caller_number, c.target_number) for c in calls]


def test_matrix_expands_in_caller_major_order() -> None:
    calls = expand(spec(["a1", "a2", "a3"], ["b1", "b2", "b3", "b4"], "matrix"))
    assert len(calls) == 12
    assert pairs(calls) == [
        (1, "a1", "b1"),
        (2, "a1", "b2"),
        (3, "a1", "b3"),
        (4, "a1", "b4"),
        (5, "a2", "b1"),
        (6, "a2", "b2"),
        (7, "a2", "b3"),
        (8, "a2", "b4"),
        (9, "a3", "b1"),
        (10, "a3", "b2"),
        (11, "a3", "b3"),
        (12, "a3", "b4"),
    ]


def test_paired_uses_shorter_side() -> None:
    calls = expand(spec(["a1", "a2", "a3"], ["b1"], "paired"))
    assert pairs(calls) == [(1, "a1", "b1")]


def test_fan_out_uses_first_caller_for_all_targets() -> None:
    calls = expand(spec(["origin", "ignored"], ["b1", "b2", "b3", "b4", "b5"], "fan-out"))
    assert pairs(calls) == [
        (1, "origin", "b1"),
        (2, "origin", "b2"),
        (3, "origin", "b3"),
        (4, "origin", "b4"),
        (5, "origin", "b5"),
    ]


def test_fan_in_uses_first_target_for_all_callers() -> None:
    calls = expand(spec(["a1", "a2", "a3", "a4", "a5"], ["dest", "ignored"], "fan-in"))
    assert pairs(calls) == [
        (1, "a1", "dest"),
        (2, "a2", "dest"),
        (3, "a3", "dest"),
        (4, "a4", "dest"),
        (5, "a5", "dest"),
    ]


def test_normalise_lines_strips_blanks_and_preserves_duplicates() -> None:
    assert normalise_lines(" 1001 \\n\\n1002\\r\\n 1001 \\n") == ["1001", "1002", "1001"]


def test_all_modes_empty_when_either_side_empty() -> None:
    for mode in ("matrix", "paired", "fan-out", "fan-in"):
        assert expand(spec([], ["b"], mode)) == []
        assert expand(spec(["a"], [], mode)) == []


def test_parallel_is_clamped_to_pjsua_max_calls() -> None:
    assert spec(["a"], ["b"], "matrix").parallel == 4
    assert TestSpec(["a"], ["b"], "matrix", "reachability", 0, 1.0, 30.0).parallel == 1
    assert TestSpec(["a"], ["b"], "matrix", "reachability", 99, 1.0, 30.0).parallel == 16
```

- [ ] **Step 2: Run the plan tests and verify they fail**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
.\.venv\Scripts\python.exe -m pytest tests\test_test_plan.py -q
```

Expected: FAIL during import with `ModuleNotFoundError: No module named 'noc_beam.testing'`.

- [ ] **Step 3: Add the package marker**

Create `python-app/src/noc_beam/testing/__init__.py`:

```python
"""Batch SIP test runner support."""
```

- [ ] **Step 4: Implement plan expansion**

Create `python-app/src/noc_beam/testing/plan.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Mode = Literal["matrix", "paired", "fan-out", "fan-in"]
PassCriterion = Literal["reachability", "full-call"]

PJSUA_MAX_CALLS = 16


@dataclass(frozen=True)
class TestSpec:
    callers: list[str]
    targets: list[str]
    mode: Mode
    pass_criterion: PassCriterion
    parallel: int
    hold_seconds: float
    timeout_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "parallel", max(1, min(PJSUA_MAX_CALLS, int(self.parallel))))
        object.__setattr__(self, "hold_seconds", max(0.0, float(self.hold_seconds)))
        object.__setattr__(self, "timeout_seconds", max(0.1, float(self.timeout_seconds)))


@dataclass(frozen=True)
class TestCall:
    index: int
    caller_number: str
    target_number: str


def normalise_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def expand(spec: TestSpec) -> list[TestCall]:
    callers = list(spec.callers)
    targets = list(spec.targets)
    if not callers or not targets:
        return []

    pairs: list[tuple[str, str]]
    if spec.mode == "matrix":
        pairs = [(caller, target) for caller in callers for target in targets]
    elif spec.mode == "paired":
        pairs = list(zip(callers, targets))
    elif spec.mode == "fan-out":
        pairs = [(callers[0], target) for target in targets]
    elif spec.mode == "fan-in":
        pairs = [(caller, targets[0]) for caller in callers]
    else:
        raise ValueError(f"Unknown test mode: {spec.mode}")

    return [
        TestCall(index=i, caller_number=caller, target_number=target)
        for i, (caller, target) in enumerate(pairs, start=1)
    ]
```

- [ ] **Step 5: Run the plan tests and full suite**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
.\.venv\Scripts\python.exe -m pytest tests\test_test_plan.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: first command passes all `test_test_plan.py` tests. Second command passes the existing suite plus the new tests.

- [ ] **Step 6: Commit Task 1**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
git add src\noc_beam\testing\__init__.py src\noc_beam\testing\plan.py tests\test_test_plan.py
git commit -m "feat: add test runner plan expansion"
```

Expected: commit succeeds.

---

### Task 2: Runner State Machine and Unit Tests

**Files:**
- Create: `python-app/src/noc_beam/testing/runner.py`
- Test: `python-app/tests/test_test_runner.py`

- [ ] **Step 1: Write failing runner tests**

Create `python-app/tests/test_test_runner.py`:

```python
from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtCore")

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from noc_beam.config.store import AccountConfig
from noc_beam.sip.events import SipEvents
from noc_beam.testing.plan import TestSpec
from noc_beam.testing.runner import TestRunner


class _Info:
    def __init__(self, call_id: int) -> None:
        self.id = call_id


class _Call:
    def __init__(self, call_id: int) -> None:
        self._info = _Info(call_id)

    def getInfo(self) -> _Info:
        return self._info


class StubEndpoint:
    def __init__(self) -> None:
        self.next_id = 100
        self.calls: dict[int, _Call] = {}
        self.hung_up: list[tuple[int, int]] = []
        self.active = 0
        self.max_active = 0
        self.raise_on_make: Exception | None = None

    def make_call(self, account_id: str, target_uri: str) -> _Call:
        if self.raise_on_make is not None:
            raise self.raise_on_make
        call = _Call(self.next_id)
        self.next_id += 1
        self.calls[call.getInfo().id] = call
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        return call

    def find_call(self, call_id: int) -> _Call | None:
        return self.calls.get(call_id)

    def hangup_call(self, call: _Call, code: int = 603) -> None:
        self.hung_up.append((call.getInfo().id, code))
        self.active = max(0, self.active - 1)


def app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def wait_until(predicate, timeout_ms: int = 1000) -> None:
    app()
    loop = QEventLoop()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)

    poll = QTimer()
    poll.setInterval(5)

    def check() -> None:
        if predicate():
            poll.stop()
            deadline.stop()
            loop.quit()

    poll.timeout.connect(check)
    deadline.start(timeout_ms)
    poll.start()
    check()
    loop.exec()
    assert predicate()


def spec(
    callers: list[str],
    targets: list[str],
    pass_criterion: str = "reachability",
    parallel: int = 4,
    hold_seconds: float = 0.01,
    timeout_seconds: float = 0.2,
) -> TestSpec:
    return TestSpec(
        callers=callers,
        targets=targets,
        mode="matrix",
        pass_criterion=pass_criterion,
        parallel=parallel,
        hold_seconds=hold_seconds,
        timeout_seconds=timeout_seconds,
    )


def account(username: str = "1001") -> AccountConfig:
    return AccountConfig(id=f"acc-{username}", username=username, domain="trunk.example.com")


def runner_for(test_spec: TestSpec, endpoint: StubEndpoint, events: SipEvents, accounts=None) -> TestRunner:
    return TestRunner(test_spec, accounts or [account()], endpoint=endpoint, events=events)


def test_reachability_passes_on_first_early_response() -> None:
    endpoint = StubEndpoint()
    events = SipEvents()
    runner = runner_for(spec(["1001"], ["2001"]), endpoint, events)
    completed = []
    runner.call_completed.connect(completed.append)

    runner.start()
    wait_until(lambda: len(endpoint.calls) == 1)
    call_id = next(iter(endpoint.calls))
    events.call_state_changed.emit("acc-1001", call_id, "EARLY", 180, "Ringing")
    wait_until(lambda: len(completed) == 1)

    result = completed[0]
    assert result.result == "PASS"
    assert result.sip_code == 180
    assert result.sip_reason == "Ringing"
    assert result.notes == ""
    assert endpoint.hung_up == [(call_id, 603)]


def test_full_call_passes_after_confirmed_and_hold_timer() -> None:
    endpoint = StubEndpoint()
    events = SipEvents()
    runner = runner_for(spec(["1001"], ["2001"], pass_criterion="full-call", hold_seconds=0.01), endpoint, events)
    completed = []
    runner.call_completed.connect(completed.append)

    runner.start()
    wait_until(lambda: len(endpoint.calls) == 1)
    call_id = next(iter(endpoint.calls))
    events.call_state_changed.emit("acc-1001", call_id, "CONFIRMED", 200, "OK")
    wait_until(lambda: len(completed) == 1)

    result = completed[0]
    assert result.result == "PASS"
    assert result.sip_code == 200
    assert result.sip_reason == "OK"
    assert endpoint.hung_up == [(call_id, 603)]


def test_sip_error_fails_call() -> None:
    endpoint = StubEndpoint()
    events = SipEvents()
    runner = runner_for(spec(["1001"], ["2001"]), endpoint, events)
    completed = []
    runner.call_completed.connect(completed.append)

    runner.start()
    wait_until(lambda: len(endpoint.calls) == 1)
    call_id = next(iter(endpoint.calls))
    events.call_state_changed.emit("acc-1001", call_id, "DISCONNECTED", 404, "Not Found")
    wait_until(lambda: len(completed) == 1)

    assert completed[0].result == "FAIL"
    assert completed[0].sip_code == 404
    assert completed[0].sip_reason == "Not Found"


def test_no_matching_account_fails_without_placing_call() -> None:
    endpoint = StubEndpoint()
    events = SipEvents()
    runner = runner_for(spec(["9999"], ["2001"]), endpoint, events, accounts=[account("1001")])
    completed = []
    runner.call_completed.connect(completed.append)

    runner.start()
    wait_until(lambda: len(completed) == 1)

    assert endpoint.calls == {}
    assert completed[0].result == "FAIL"
    assert completed[0].sip_code == 0
    assert completed[0].notes == "no matching account"


def test_timeout_fails_with_408() -> None:
    endpoint = StubEndpoint()
    events = SipEvents()
    runner = runner_for(spec(["1001"], ["2001"], timeout_seconds=0.01), endpoint, events)
    completed = []
    runner.call_completed.connect(completed.append)

    runner.start()
    wait_until(lambda: len(completed) == 1)

    assert completed[0].result == "FAIL"
    assert completed[0].sip_code == 408
    assert completed[0].sip_reason == "Request Timeout"
    assert completed[0].notes == "timeout"


def test_parallel_limit_never_exceeds_two_active_calls() -> None:
    endpoint = StubEndpoint()
    events = SipEvents()
    runner = runner_for(spec(["1001"], ["2001", "2002", "2003", "2004"], parallel=2), endpoint, events)
    completed = []
    runner.call_completed.connect(completed.append)

    runner.start()
    wait_until(lambda: len(endpoint.calls) == 2)
    assert endpoint.max_active == 2

    for call_id in list(endpoint.calls)[:2]:
        events.call_state_changed.emit("acc-1001", call_id, "EARLY", 180, "Ringing")
    wait_until(lambda: len(endpoint.calls) == 4)

    assert endpoint.max_active == 2
    for call_id in list(endpoint.calls)[2:]:
        events.call_state_changed.emit("acc-1001", call_id, "EARLY", 180, "Ringing")
    wait_until(lambda: len(completed) == 4)


def test_cancel_fails_in_flight_and_queued_calls() -> None:
    endpoint = StubEndpoint()
    events = SipEvents()
    runner = runner_for(spec(["1001"], ["2001", "2002", "2003"], parallel=1), endpoint, events)
    completed = []
    runner.call_completed.connect(completed.append)

    runner.start()
    wait_until(lambda: len(endpoint.calls) == 1)
    runner.cancel()
    wait_until(lambda: len(completed) == 3)

    assert [r.result for r in completed] == ["FAIL", "FAIL", "FAIL"]
    assert [r.notes for r in completed] == ["cancelled", "cancelled", "cancelled"]
    assert len(endpoint.hung_up) == 1
```

- [ ] **Step 2: Run runner tests and verify they fail**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
.\.venv\Scripts\python.exe -m pytest tests\test_test_runner.py -q
```

Expected: FAIL during import with `ModuleNotFoundError: No module named 'noc_beam.testing.runner'`.

- [ ] **Step 3: Implement runner**

Create `python-app/src/noc_beam/testing/runner.py`:

```python
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
    pjsua_call: object
    call_id: int
    target_uri: str
    started_at: float
    timeout_timer: QTimer
    done: bool = False


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
        self.accounts = list(accounts)
        self.endpoint = endpoint if endpoint is not None else SipEndpoint.instance()
        self.events = events if events is not None else sip_events()
        self._queue = deque(expand(spec))
        self._total = len(self._queue)
        self._active: dict[int, _ActiveCall] = {}
        self._results: list[TestResult] = []
        self._running = False
        self._cancelled = False
        self.events.call_state_changed.connect(self._on_call_state_changed)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        QTimer.singleShot(0, self._drain_queue)

    def cancel(self) -> None:
        if not self._running:
            return
        self._cancelled = True
        queued = list(self._queue)
        self._queue.clear()
        for active in list(self._active.values()):
            self._finish(active, "FAIL", 0, "Cancelled", "cancelled", hangup=True)
        for call in queued:
            self._emit_result(
                TestResult(
                    call=call,
                    result="FAIL",
                    sip_code=0,
                    sip_reason="Cancelled",
                    rtt_ms=None,
                    duration_s=0.0,
                    notes="cancelled",
                    started_at=time.time(),
                    from_account=call.caller_number,
                    to_uri=call.target_number,
                )
            )
        self._maybe_complete()

    def _drain_queue(self) -> None:
        if self._cancelled:
            return
        while self._queue and len(self._active) < self.spec.parallel:
            self._start_call(self._queue.popleft())
        self._maybe_complete()

    def _start_call(self, call: TestCall) -> None:
        account = self._account_for(call.caller_number)
        started_at = time.time()
        if account is None:
            self._emit_result(
                TestResult(call, "FAIL", 0, "No matching account", None, 0.0, "no matching account", started_at, call.caller_number, call.target_number)
            )
            return

        target_uri = self._target_uri(call.target_number, account)
        try:
            pjsua_call = self.endpoint.make_call(account.id, target_uri)
            call_id = int(pjsua_call.getInfo().id)
        except Exception as exc:
            note = "pjsua2 not available" if "pjsua2 not available" in str(exc).lower() else str(exc)
            self._emit_result(
                TestResult(call, "FAIL", 0, "Endpoint error", None, 0.0, note, started_at, account.username, target_uri)
            )
            return

        timer = QTimer(self)
        timer.setSingleShot(True)
        active = _ActiveCall(call, account, pjsua_call, call_id, target_uri, started_at, timer)
        self._active[call_id] = active
        timer.timeout.connect(lambda call_id=call_id: self._on_timeout(call_id))
        timer.start(int(self.spec.timeout_seconds * 1000))
        self.call_started.emit(call.index)

    def _on_call_state_changed(self, account_id: str, call_id: int, state: str, code: int, reason: str) -> None:
        active = self._active.get(call_id)
        if active is None or active.done:
            return
        if code >= 400:
            self._finish(active, "FAIL", code, reason, "")
            return
        if self.spec.pass_criterion == "reachability" and state == "EARLY" and code in (180, 183):
            self._finish(active, "PASS", code, reason, "", hangup=True)
            return
        if self.spec.pass_criterion == "full-call" and state == "CONFIRMED":
            QTimer.singleShot(int(self.spec.hold_seconds * 1000), lambda call_id=call_id, code=code, reason=reason: self._finish_full_call(call_id, code, reason))
            return
        if state == "DISCONNECTED":
            fail_code = code or 0
            fail_reason = reason or "Disconnected"
            self._finish(active, "FAIL", fail_code, fail_reason, "")

    def _finish_full_call(self, call_id: int, code: int, reason: str) -> None:
        active = self._active.get(call_id)
        if active is None or active.done:
            return
        self._finish(active, "PASS", code or 200, reason or "OK", "", hangup=True)

    def _on_timeout(self, call_id: int) -> None:
        active = self._active.get(call_id)
        if active is None or active.done:
            return
        self._finish(active, "FAIL", 408, "Request Timeout", "timeout", hangup=True)

    def _finish(
        self,
        active: _ActiveCall,
        result: Literal["PASS", "FAIL"],
        sip_code: int | None,
        sip_reason: str,
        notes: str,
        *,
        hangup: bool = False,
    ) -> None:
        if active.done:
            return
        active.done = True
        active.timeout_timer.stop()
        if hangup:
            try:
                self.endpoint.hangup_call(active.pjsua_call)
            except Exception:
                pass
        self._active.pop(active.call_id, None)
        now = time.time()
        rtt_ms = (now - active.started_at) * 1000.0
        self._emit_result(
            TestResult(
                call=active.call,
                result=result,
                sip_code=sip_code,
                sip_reason=sip_reason,
                rtt_ms=rtt_ms,
                duration_s=now - active.started_at,
                notes=notes,
                started_at=active.started_at,
                from_account=active.account.username,
                to_uri=active.target_uri,
            )
        )
        self._drain_queue()

    def _emit_result(self, result: TestResult) -> None:
        self._results.append(result)
        self.call_completed.emit(result)

    def _maybe_complete(self) -> None:
        if self._running and len(self._results) >= self._total and not self._active and not self._queue:
            self._running = False
            self.run_complete.emit(list(self._results))

    def _account_for(self, caller_number: str) -> AccountConfig | None:
        return next((account for account in self.accounts if account.username == caller_number), None)

    @staticmethod
    def _target_uri(target_number: str, account: AccountConfig) -> str:
        if target_number.startswith(("sip:", "sips:", "tel:")):
            return target_number
        if "@" in target_number:
            return f"sip:{target_number}"
        return f"sip:{target_number}@{account.domain}"
```

- [ ] **Step 4: Run runner tests**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
.\.venv\Scripts\python.exe -m pytest tests\test_test_runner.py -q
```

Expected: all runner tests pass.

- [ ] **Step 5: Run the full suite**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: full suite passes.

- [ ] **Step 6: Commit Task 2**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
git add src\noc_beam\testing\runner.py tests\test_test_runner.py
git commit -m "feat: add SIP test runner state machine"
```

Expected: commit succeeds.

---

### Task 3: Qt Test Runner Window and CSV Export

**Files:**
- Create: `python-app/src/noc_beam/ui/test_runner_view.py`
- Test: `python-app/tests/test_test_runner_view.py`

- [ ] **Step 1: Write failing UI smoke tests**

Create `python-app/tests/test_test_runner_view.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from noc_beam.config.store import AccountConfig
from noc_beam.testing.plan import TestCall
from noc_beam.testing.runner import TestResult
from noc_beam.ui.test_runner_view import TestRunnerView


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_view_constructs_with_empty_accounts(qapp) -> None:
    view = TestRunnerView([])
    assert view.windowTitle() == "NOC_Beam test runner"
    assert view.run_btn.isEnabled() is False


def test_run_button_count_updates_from_inputs(qapp) -> None:
    view = TestRunnerView([AccountConfig(id="a1", username="1001", domain="trunk.example.com")])
    view.callers_edit.setPlainText("1001\\n1002\\n")
    view.targets_edit.setPlainText("2001\\n2002\\n2003\\n")
    view.mode_combo.setCurrentText("Matrix")
    view._refresh_plan_preview()
    assert view.run_btn.text() == "Run 6 calls"
    assert view.run_btn.isEnabled() is True


def test_hold_spinner_only_enabled_for_full_call(qapp) -> None:
    view = TestRunnerView([])
    view.pass_combo.setCurrentText("Reachability")
    view._refresh_hold_enabled()
    assert view.hold_spin.isEnabled() is False
    view.pass_combo.setCurrentText("Full call")
    view._refresh_hold_enabled()
    assert view.hold_spin.isEnabled() is True


def test_export_csv_writes_documented_header_and_rows(qapp, tmp_path: Path) -> None:
    view = TestRunnerView([])
    result = TestResult(
        call=TestCall(index=1, caller_number="1001", target_number="2001"),
        result="PASS",
        sip_code=180,
        sip_reason="Ringing",
        rtt_ms=240.0,
        duration_s=1.2,
        notes="",
        started_at=1778838330.0,
        from_account="1001",
        to_uri="sip:2001@trunk.example.com",
    )
    view._on_call_completed(result)
    out = tmp_path / "runner.csv"
    view.export_csv(out)

    assert out.read_text(encoding="utf-8", newline="") == (
        "test_run_id,started_at,from_account,to_uri,result,sip_code,sip_reason,rtt_ms,duration_s,notes\\n"
        "nb-20260515-004530-001,2026-05-15T00:45:30Z,1001,sip:2001@trunk.example.com,PASS,180,Ringing,240,1.2,\\n"
    )
```

- [ ] **Step 2: Run UI tests and verify they fail**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
$env:QT_QPA_PLATFORM="offscreen"
.\.venv\Scripts\python.exe -m pytest tests\test_test_runner_view.py -q
```

Expected: FAIL during import with `ModuleNotFoundError: No module named 'noc_beam.ui.test_runner_view'`.

- [ ] **Step 3: Implement `TestRunnerView`**

Create `python-app/src/noc_beam/ui/test_runner_view.py` with these public attributes used by tests: `callers_edit`, `targets_edit`, `mode_combo`, `pass_combo`, `parallel_spin`, `hold_spin`, `timeout_spin`, `run_btn`, `table`, `summary_label`, `cancel_btn`, `export_btn`.

The file should contain:

```python
from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.store import AccountConfig
from noc_beam.testing.plan import TestSpec, expand, normalise_lines
from noc_beam.testing.runner import TestResult, TestRunner


MODE_LABELS = {
    "Matrix": "matrix",
    "Paired": "paired",
    "Fan-out": "fan-out",
    "Fan-in": "fan-in",
}

PASS_LABELS = {
    "Reachability": "reachability",
    "Full call": "full-call",
}


class TestRunnerView(QMainWindow):
    def __init__(self, accounts: list[AccountConfig], parent=None) -> None:
        super().__init__(parent)
        self.accounts = list(accounts)
        self.runner: TestRunner | None = None
        self.results: list[TestResult] = []
        self._row_by_call_index: dict[int, int] = {}
        self.setWindowTitle("NOC_Beam test runner")
        self.resize(900, 620)
        self._build_ui()
        self._connect_ui()
        self._refresh_hold_enabled()
        self._refresh_plan_preview()

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        paste_grid = QGridLayout()
        paste_grid.addWidget(QLabel("CALLERS"), 0, 0)
        paste_grid.addWidget(QLabel("TARGETS"), 0, 1)
        self.callers_edit = QTextEdit()
        self.targets_edit = QTextEdit()
        self.callers_edit.setAcceptRichText(False)
        self.targets_edit.setAcceptRichText(False)
        paste_grid.addWidget(self.callers_edit, 1, 0)
        paste_grid.addWidget(self.targets_edit, 1, 1)
        layout.addLayout(paste_grid)

        controls = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(MODE_LABELS.keys())
        self.pass_combo = QComboBox()
        self.pass_combo.addItems(PASS_LABELS.keys())
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 16)
        self.parallel_spin.setValue(4)
        self.hold_spin = QDoubleSpinBox()
        self.hold_spin.setRange(0.0, 300.0)
        self.hold_spin.setValue(2.0)
        self.hold_spin.setSuffix(" s")
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0.1, 300.0)
        self.timeout_spin.setValue(30.0)
        self.timeout_spin.setSuffix(" s")
        for label, widget in (
            ("Mode", self.mode_combo),
            ("Pass", self.pass_combo),
            ("Parallel", self.parallel_spin),
            ("Hold", self.hold_spin),
            ("Timeout", self.timeout_spin),
        ):
            controls.addWidget(QLabel(label))
            controls.addWidget(widget)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.run_btn = QPushButton("Run 0 calls")
        layout.addWidget(self.run_btn)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["#", "FROM", "TO", "RESULT", "CODE", "RTT", "TIME", "notes"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.summary_label = QLabel("0 passed · 0 failed · 0 running")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.export_btn = QPushButton("Export CSV")
        self.export_btn.setEnabled(False)
        footer.addWidget(self.summary_label)
        footer.addStretch(1)
        footer.addWidget(self.cancel_btn)
        footer.addWidget(self.export_btn)
        layout.addLayout(footer)

        self.setCentralWidget(central)

    def _connect_ui(self) -> None:
        self.callers_edit.textChanged.connect(self._refresh_plan_preview)
        self.targets_edit.textChanged.connect(self._refresh_plan_preview)
        self.mode_combo.currentTextChanged.connect(self._refresh_plan_preview)
        self.pass_combo.currentTextChanged.connect(self._refresh_hold_enabled)
        self.pass_combo.currentTextChanged.connect(self._refresh_plan_preview)
        self.parallel_spin.valueChanged.connect(self._refresh_plan_preview)
        self.hold_spin.valueChanged.connect(self._refresh_plan_preview)
        self.timeout_spin.valueChanged.connect(self._refresh_plan_preview)
        self.run_btn.clicked.connect(self._on_run_clicked)
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        self.export_btn.clicked.connect(self._on_export_clicked)

    def _spec_from_ui(self) -> TestSpec:
        return TestSpec(
            callers=normalise_lines(self.callers_edit.toPlainText()),
            targets=normalise_lines(self.targets_edit.toPlainText()),
            mode=MODE_LABELS[self.mode_combo.currentText()],
            pass_criterion=PASS_LABELS[self.pass_combo.currentText()],
            parallel=self.parallel_spin.value(),
            hold_seconds=self.hold_spin.value(),
            timeout_seconds=self.timeout_spin.value(),
        )

    def _refresh_hold_enabled(self) -> None:
        self.hold_spin.setEnabled(PASS_LABELS[self.pass_combo.currentText()] == "full-call")

    def _refresh_plan_preview(self) -> None:
        count = len(expand(self._spec_from_ui()))
        self.run_btn.setText(f"Run {count} calls")
        self.run_btn.setEnabled(count > 0 and self.runner is None)

    def _on_run_clicked(self) -> None:
        spec = self._spec_from_ui()
        calls = expand(spec)
        if not calls:
            return
        self.results.clear()
        self._row_by_call_index.clear()
        self.table.setRowCount(0)
        for call in calls:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._row_by_call_index[call.index] = row
            for col, value in enumerate((call.index, call.caller_number, call.target_number, "queued", "", "", "", "")):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        self.runner = TestRunner(spec, self.accounts, self)
        self.runner.call_started.connect(self._on_call_started)
        self.runner.call_completed.connect(self._on_call_completed)
        self.runner.run_complete.connect(self._on_run_complete)
        self.cancel_btn.setEnabled(True)
        self.export_btn.setEnabled(False)
        self._refresh_summary()
        self.runner.start()
        self._refresh_plan_preview()

    def _on_call_started(self, call_index: int) -> None:
        row = self._row_by_call_index.get(call_index)
        if row is not None:
            self.table.setItem(row, 3, QTableWidgetItem("running"))
        self._refresh_summary()

    def _on_call_completed(self, result: TestResult) -> None:
        self.results.append(result)
        row = self._row_by_call_index.get(result.call.index)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._row_by_call_index[result.call.index] = row
        values = [
            result.call.index,
            result.call.caller_number,
            result.to_uri,
            result.result,
            "" if result.sip_code is None else result.sip_code,
            "" if result.rtt_ms is None else int(result.rtt_ms),
            f"{result.duration_s:.1f}",
            result.notes,
        ]
        for col, value in enumerate(values):
            self.table.setItem(row, col, QTableWidgetItem(str(value)))
        self.export_btn.setEnabled(bool(self.results))
        self._refresh_summary()

    def _on_run_complete(self, _results: list[TestResult]) -> None:
        self.runner = None
        self.cancel_btn.setEnabled(False)
        self._refresh_plan_preview()
        self._refresh_summary()

    def _on_cancel_clicked(self) -> None:
        if self.runner is not None:
            self.runner.cancel()

    def _on_export_clicked(self) -> None:
        path, _selected_filter = QFileDialog.getSaveFileName(self, "Export CSV", "test-runner.csv", "CSV files (*.csv)")
        if path:
            self.export_csv(Path(path))

    def export_csv(self, path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["test_run_id", "started_at", "from_account", "to_uri", "result", "sip_code", "sip_reason", "rtt_ms", "duration_s", "notes"])
            for result in self.results:
                started = datetime.fromtimestamp(result.started_at, UTC)
                run_id = f"nb-{started:%Y%m%d-%H%M%S}-{result.call.index:03d}"
                writer.writerow([
                    run_id,
                    started.isoformat().replace("+00:00", "Z"),
                    result.from_account,
                    result.to_uri,
                    result.result,
                    "" if result.sip_code is None else result.sip_code,
                    result.sip_reason,
                    "" if result.rtt_ms is None else int(result.rtt_ms),
                    f"{result.duration_s:.1f}",
                    result.notes,
                ])

    def _refresh_summary(self) -> None:
        passed = sum(1 for result in self.results if result.result == "PASS")
        failed = sum(1 for result in self.results if result.result == "FAIL")
        running = sum(1 for row in range(self.table.rowCount()) if self.table.item(row, 3) and self.table.item(row, 3).text() == "running")
        self.summary_label.setText(f"{passed} passed · {failed} failed · {running} running")
```

- [ ] **Step 4: Run UI tests**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
$env:QT_QPA_PLATFORM="offscreen"
.\.venv\Scripts\python.exe -m pytest tests\test_test_runner_view.py -q
```

Expected: all UI tests pass.

- [ ] **Step 5: Run plan, runner, and UI tests together**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
$env:QT_QPA_PLATFORM="offscreen"
.\.venv\Scripts\python.exe -m pytest tests\test_test_plan.py tests\test_test_runner.py tests\test_test_runner_view.py -q
```

Expected: all new tests pass.

- [ ] **Step 6: Commit Task 3**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
git add src\noc_beam\ui\test_runner_view.py tests\test_test_runner_view.py
git commit -m "feat: add test runner window"
```

Expected: commit succeeds.

---

### Task 4: Hamburger Menu Wiring

**Files:**
- Modify: `python-app/src/noc_beam/ui/phone_shell.py`
- Test: extend `python-app/tests/test_test_runner_view.py`

- [ ] **Step 1: Add a wiring smoke test**

Append this test to `python-app/tests/test_test_runner_view.py`:

```python
def test_phone_shell_has_test_runner_menu_action(qapp) -> None:
    from noc_beam.ui.phone_shell import PhoneShell

    shell = PhoneShell()
    view_items = dict(shell._menu_actions)["View"]
    labels = [label for label, _slot in view_items]
    assert "Test Runner..." in labels
```

- [ ] **Step 2: Run the new smoke test and verify it fails**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
$env:QT_QPA_PLATFORM="offscreen"
.\.venv\Scripts\python.exe -m pytest tests\test_test_runner_view.py::test_phone_shell_has_test_runner_menu_action -q
```

Expected: FAIL with assertion that `"Test Runner..."` is missing.

- [ ] **Step 3: Add the View menu entry**

Modify the View group inside `PhoneShell._build_menu()` in `python-app/src/noc_beam/ui/phone_shell.py`:

```python
            ("View", [
                ("NOC Trace...",              self._on_open_trace),
                ("NOC Accounts...",           self._on_open_accounts),
                ("Test Runner...",            self._on_open_test_runner),
                ("Diagnostics...",            self._on_diagnostics),
                ("---", None),
                ("Open wide dashboard...",    self._on_open_wide),
            ]),
```

- [ ] **Step 4: Add the lazy window opener**

Add this method near `_on_open_accounts()` in `python-app/src/noc_beam/ui/phone_shell.py`:

```python
    def _on_open_test_runner(self):
        from noc_beam.ui.test_runner_view import TestRunnerView
        if not hasattr(self, "_test_runner_window"):
            self._test_runner_window = TestRunnerView(self.accounts, self)
            self._test_runner_window.resize(900, 620)
        self._test_runner_window.accounts = list(self.accounts)
        self._test_runner_window.show()
        self._test_runner_window.raise_(); self._test_runner_window.activateWindow()
```

- [ ] **Step 5: Run the menu smoke test**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
$env:QT_QPA_PLATFORM="offscreen"
.\.venv\Scripts\python.exe -m pytest tests\test_test_runner_view.py::test_phone_shell_has_test_runner_menu_action -q
```

Expected: test passes.

- [ ] **Step 6: Run the full suite**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
$env:QT_QPA_PLATFORM="offscreen"
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: full suite passes.

- [ ] **Step 7: Commit Task 4**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
git add src\noc_beam\ui\phone_shell.py tests\test_test_runner_view.py
git commit -m "feat: wire test runner into phone shell"
```

Expected: commit succeeds.

---

### Task 5: Manual Smoke and Stub-Mode Verification

**Files:**
- No new source files.
- Use existing `python-app/src/noc_beam/__main__.py` entry point.

- [ ] **Step 1: Run the application in UI-only local mode**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
.\.venv\Scripts\python.exe -m noc_beam
```

Expected: main `NOC_Beam` window opens. If pjsua2 is unavailable, the status banner may report an endpoint error, but the application stays open.

- [ ] **Step 2: Open the Test Runner window**

Manual action: click hamburger menu, then `Test Runner...`.

Expected: separate `NOC_Beam test runner` window opens at roughly `900 x 620` with caller/target paste boxes, controls, run button, results grid, and footer.

- [ ] **Step 3: Verify live count and empty disable**

Manual action:

1. Leave both paste boxes empty.
2. Confirm run button reads `Run 0 calls` and is disabled.
3. Paste `1001` into CALLERS and `2001\n2002\n2003` into TARGETS.
4. Select `Matrix`.

Expected: run button reads `Run 3 calls` and is enabled.

- [ ] **Step 4: Verify stub-mode failures complete cleanly**

Manual action: with no matching configured account for `1001`, click Run.

Expected: each row becomes `FAIL`, `CODE` is `0`, notes include `no matching account`, footer failed count matches row count, Cancel disables when complete, Export CSV enables.

- [ ] **Step 5: Verify CSV export manually**

Manual action: click `Export CSV` and save to a temporary path such as `E:\NOC_Beam\Eyebeam\python-app\test-runner-smoke.csv`.

Expected: file starts with:

```csv
test_run_id,started_at,from_account,to_uri,result,sip_code,sip_reason,rtt_ms,duration_s,notes
```

Expected: file has one row per displayed result, UTF-8 encoding, and `\n` line endings.

- [ ] **Step 6: Remove the manual smoke CSV**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
Remove-Item -LiteralPath .\test-runner-smoke.csv -ErrorAction SilentlyContinue
```

Expected: command succeeds. This removes only the temporary smoke file.

- [ ] **Step 7: Run final automated verification**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
$env:QT_QPA_PLATFORM="offscreen"
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: full suite passes.

- [ ] **Step 8: Commit any smoke-fix changes**

If the manual smoke found a defect and code was changed, run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
git add src\noc_beam\testing src\noc_beam\ui tests
git commit -m "fix: polish test runner smoke issues"
```

Expected: commit succeeds only when there are actual smoke-fix changes. If there are no changes, skip this step.

---

### Task 6: Push Feature Branch

**Files:**
- No source changes.

- [ ] **Step 1: Check branch and worktree**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
git branch --show-current
git status --short
```

Expected: branch is `claude/debug-error-Dlc4I` or the active feature branch requested by the user. Status is clean except for unrelated existing files outside the committed feature work.

- [ ] **Step 2: Push commits**

Run:

```powershell
cd E:\NOC_Beam\Eyebeam\python-app
git push origin claude/debug-error-Dlc4I
```

Expected: push succeeds.

---

## Self-Review

**Spec coverage:**
- Matrix, paired, fan-out, and fan-in are covered by Task 1 tests and implementation.
- Blank stripping and duplicate preservation are covered by Task 1.
- `parallel` cap at 16 is covered by Task 1 and enforced again by the UI spinner in Task 3.
- Caller resolution by `AccountConfig.username` and target URI building are covered by Task 2.
- Reachability, full-call, SIP error, no-account, timeout, concurrency, and cancel behavior are covered by Task 2.
- Separate Qt window, paste boxes, controls, count button, grid, footer, cancel, and CSV export are covered by Task 3.
- Hamburger menu entry and lazy window opening are covered by Task 4.
- Stub-mode exception handling is covered by Task 2 through endpoint exception logic and Task 5 manually.

**Placeholder scan:**
- No placeholder markers or vague implementation-only tasks remain.
- Code-facing steps include concrete code blocks and exact commands.

**Type consistency:**
- `TestSpec`, `TestCall`, `TestResult`, and `TestRunner` fields are consistent across plan, runner, UI, and tests.
- Mode and pass-criterion string values match the original spec.
- `AccountConfig.username`, `AccountConfig.id`, and `AccountConfig.domain` match the current `noc_beam.config.store` dataclass.
