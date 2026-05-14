"""State-machine + registry behaviour for CallManager."""
from __future__ import annotations

import pytest

# Skip if PySide6 can't initialise its Qt platform (headless CI without libEGL).
pytest.importorskip("PySide6.QtCore")

from noc_beam.sip.call_manager import (  # noqa: E402
    CallManager,
    CallRecord,
    CallState,
    is_legal_transition,
)


def test_legal_transitions_table() -> None:
    # A handful of sanity checks — not exhaustive.
    assert is_legal_transition(CallState.NULL, CallState.CALLING)
    assert is_legal_transition(CallState.CALLING, CallState.EARLY)
    assert is_legal_transition(CallState.CONFIRMED, CallState.HELD)
    assert is_legal_transition(CallState.HELD, CallState.CONFIRMED)
    assert is_legal_transition(CallState.HELD, CallState.DISCONNECTED)
    assert is_legal_transition(CallState.CONFIRMED, CallState.DISCONNECTED)


def test_illegal_transitions_blocked() -> None:
    assert not is_legal_transition(CallState.DISCONNECTED, CallState.CONFIRMED)
    assert not is_legal_transition(CallState.NULL, CallState.HELD)
    assert not is_legal_transition(CallState.CALLING, CallState.HELD)
    assert not is_legal_transition(CallState.HELD, CallState.EARLY)


def test_register_and_update_flow() -> None:
    mgr = CallManager()
    rec = CallRecord(call_id=42, account_id="acc1", direction="out", remote_uri="sip:bob@x")
    mgr.register(rec)
    assert mgr.get(42) is rec

    # Fresh records are NULL — not yet "active" until first transition.
    assert mgr.update_state(42, CallState.CALLING)
    assert mgr.get(42).state == CallState.CALLING
    assert len(mgr.active()) == 1

    assert mgr.update_state(42, CallState.CONFIRMED)
    assert mgr.get(42).connected_at is not None


def test_illegal_transition_is_dropped_not_raised() -> None:
    mgr = CallManager()
    mgr.register(CallRecord(call_id=1, account_id="a", state=CallState.CONFIRMED))
    # Force-set connected_at so duration logic doesn't trip.
    mgr.get(1).connected_at = 1.0
    # CONFIRMED -> EARLY is not legal — should return False, not raise.
    assert mgr.update_state(1, CallState.EARLY) is False
    assert mgr.get(1).state == CallState.CONFIRMED


def test_disconnected_removes_record() -> None:
    mgr = CallManager()
    mgr.register(CallRecord(call_id=7, account_id="a", state=CallState.CONFIRMED))
    mgr.get(7).connected_at = 1.0

    removed: list[int] = []
    mgr.call_removed.connect(removed.append)

    assert mgr.update_state(7, CallState.DISCONNECTED, code=487, reason="Cancelled")
    assert mgr.get(7) is None
    assert removed == [7]


def test_hold_and_resume_toggle_on_hold_flag() -> None:
    mgr = CallManager()
    mgr.register(CallRecord(call_id=3, account_id="a", state=CallState.CONFIRMED))
    mgr.get(3).connected_at = 1.0

    mgr.update_state(3, CallState.HELD)
    assert mgr.get(3).on_hold is True

    mgr.update_state(3, CallState.CONFIRMED)
    assert mgr.get(3).on_hold is False


def test_multi_call_registry_independent() -> None:
    mgr = CallManager()
    mgr.register(CallRecord(call_id=10, account_id="a", direction="out"))
    mgr.register(CallRecord(call_id=11, account_id="b", direction="in"))
    mgr.update_state(10, CallState.CALLING)
    mgr.update_state(11, CallState.INCOMING)

    assert {r.call_id for r in mgr.active()} == {10, 11}
    assert mgr.get(10).account_id == "a"
    assert mgr.get(11).account_id == "b"


def test_duplicate_register_is_noop() -> None:
    mgr = CallManager()
    mgr.register(CallRecord(call_id=1, account_id="a"))
    # Duplicate id: logged warning, original record preserved.
    mgr.register(CallRecord(call_id=1, account_id="b"))
    assert mgr.get(1).account_id == "a"


def test_update_unknown_call_is_noop() -> None:
    mgr = CallManager()
    assert mgr.update_state(999, CallState.CONFIRMED) is False
    mgr.update_media(999, "opus", 48000, 1)  # should not raise
    mgr.set_mute(999, True)
