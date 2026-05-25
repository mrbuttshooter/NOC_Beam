from __future__ import annotations

import logging
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtCore = pytest.importorskip("PySide6.QtCore")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

# A QApplication must exist before QObject/QTimer construction (the
# RegistrationRetry inherits QObject and owns QTimers).
_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

from noc_beam.sip import registration_retry  # noqa: E402
from noc_beam.sip.registration_retry import (  # noqa: E402
    LONG_SLEEP_INTERVAL_MS,
    MAX_FAST_RETRIES,
    RegistrationRetry,
)


def test_register_method_not_allowed_is_not_retried() -> None:
    assert 405 in registration_retry._NO_RETRY_CODES


@pytest.fixture
def fake_timer(monkeypatch: pytest.MonkeyPatch):
    """Replace QTimer with a recorder so _schedule_retry doesn't actually
    fire the retry callback or block the test on event-loop ticks.

    Each call to QTimer().start(ms) is captured into `intervals`. timeout
    is a real Signal so .connect() still works."""

    intervals: list[int] = []

    class _FakeTimer(QtCore.QObject):
        # The real QTimer.timeout is a Signal; subclassing QObject and
        # declaring one keeps `timer.timeout.connect(...)` working.
        timeout = QtCore.Signal()

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._single = False

        def setSingleShot(self, val: bool) -> None:
            self._single = val

        def start(self, ms: int) -> None:
            intervals.append(int(ms))

        def stop(self) -> None:
            pass

    monkeypatch.setattr(registration_retry, "QTimer", _FakeTimer)
    return intervals


def test_switches_to_long_sleep_after_max_fast_retries(
    fake_timer: list[int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After MAX_FAST_RETRIES sustained failures the next scheduled
    interval is LONG_SLEEP_INTERVAL_MS, not the 30 s cap."""
    rr = RegistrationRetry()
    try:
        with caplog.at_level(logging.WARNING, logger="noc_beam.sip.registration_retry"):
            # Drive MAX_FAST_RETRIES + 2 failures through the same account.
            # The first MAX_FAST_RETRIES should follow the fast-backoff
            # ladder (capped at 30 000 ms); the (MAX_FAST_RETRIES+1)-th
            # and onward should use the long-sleep interval.
            for _ in range(MAX_FAST_RETRIES + 2):
                rr._schedule_retry("acct-x", 503, "Service Unavailable")
    finally:
        rr.deleteLater()

    # First MAX_FAST_RETRIES intervals: all fast (<= 30 000 ms).
    fast = fake_timer[:MAX_FAST_RETRIES]
    assert all(ms <= 30_000 for ms in fast), fast

    # Remaining intervals are the long-sleep value.
    long_tail = fake_timer[MAX_FAST_RETRIES:]
    assert long_tail, "expected at least one long-sleep retry to be scheduled"
    assert all(ms == LONG_SLEEP_INTERVAL_MS for ms in long_tail), long_tail

    # The transition log line fires exactly once.
    transition_lines = [
        r for r in caplog.records
        if "switching to long-sleep retry" in r.getMessage()
    ]
    assert len(transition_lines) == 1, [r.getMessage() for r in transition_lines]
    assert "MAX_FAST_RETRIES=60" in transition_lines[0].getMessage()


def test_long_sleep_continues_indefinitely(fake_timer: list[int]) -> None:
    """Once in long-sleep mode, every subsequent retry uses
    LONG_SLEEP_INTERVAL_MS — we never silently give up, because carriers
    sometimes recover after hours."""
    rr = RegistrationRetry()
    try:
        # 200 attempts past the cutoff is still long-sleep.
        for _ in range(MAX_FAST_RETRIES + 200):
            rr._schedule_retry("acct-y", 503, "Service Unavailable")
    finally:
        rr.deleteLater()

    tail = fake_timer[MAX_FAST_RETRIES:]
    assert len(tail) == 200
    assert all(ms == LONG_SLEEP_INTERVAL_MS for ms in tail)


def test_reset_clears_attempt_counter_so_long_sleep_doesnt_stick(
    fake_timer: list[int],
) -> None:
    """A successful registration (which calls _reset) must drop the
    account back to the fast ladder; otherwise a single recovery would
    leave the account permanently throttled."""
    rr = RegistrationRetry()
    try:
        # Push the account into long-sleep.
        for _ in range(MAX_FAST_RETRIES + 3):
            rr._schedule_retry("acct-z", 503, "Service Unavailable")
        assert fake_timer[-1] == LONG_SLEEP_INTERVAL_MS

        # Simulate a 2xx clearing the schedule.
        rr._reset("acct-z")
        fake_timer.clear()

        # The next failure should be back at the start of the fast
        # ladder (1 000 ms), not at long-sleep.
        rr._schedule_retry("acct-z", 503, "Service Unavailable")
        assert fake_timer == [1000]
    finally:
        rr.deleteLater()
