"""Multi-call registry and call-level state machine.

Replaces the old single-`_active_call` slot in MainWindow. Every active call
gets a `CallRecord` keyed by its pjsua2 call-id; the manager emits Qt signals
when a record is added / updated / removed so any widget can stay in sync.

State machine is intentionally strict: illegal transitions are dropped with a
log warning, never raised — PJSIP can emit duplicate or out-of-order callbacks
on disconnect and we don't want to crash the UI over them.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class CallState(str, Enum):
    NULL = "NULL"
    CALLING = "CALLING"            # outbound, INVITE sent
    INCOMING = "INCOMING"          # inbound, INVITE received
    EARLY = "EARLY"                # 18x received
    CONNECTING = "CONNECTING"      # 200 sent/received, awaiting ACK
    CONFIRMED = "CONFIRMED"        # media active
    HELD = "HELD"                  # local hold (sendonly / inactive)
    DISCONNECTED = "DISCONNECTED"


# Allowed forward transitions. Anything not listed is dropped.
_TRANSITIONS: dict[CallState, set[CallState]] = {
    CallState.NULL: {CallState.CALLING, CallState.INCOMING, CallState.DISCONNECTED},
    CallState.CALLING: {CallState.EARLY, CallState.CONNECTING, CallState.CONFIRMED, CallState.DISCONNECTED},
    CallState.INCOMING: {CallState.EARLY, CallState.CONNECTING, CallState.CONFIRMED, CallState.DISCONNECTED},
    CallState.EARLY: {CallState.CONNECTING, CallState.CONFIRMED, CallState.DISCONNECTED},
    CallState.CONNECTING: {CallState.CONFIRMED, CallState.DISCONNECTED},
    CallState.CONFIRMED: {CallState.HELD, CallState.DISCONNECTED},
    CallState.HELD: {CallState.CONFIRMED, CallState.DISCONNECTED},
    CallState.DISCONNECTED: set(),
}


def is_legal_transition(prev: CallState, nxt: CallState) -> bool:
    """Is `prev -> nxt` allowed by the call state machine?"""
    if prev == nxt:
        return True
    return nxt in _TRANSITIONS.get(prev, set())


@dataclass
class CallRecord:
    """User-facing snapshot of a call. Owned by CallManager."""

    call_id: int
    account_id: str
    account_label: str = ""          # human label for the account_id
    remote_uri: str = ""
    dialed_uri: str = ""             # user-facing dial target, before routing prefixes
    supplier_id: str = ""
    supplier_label: str = ""
    direction: str = "out"          # "in" | "out"
    state: CallState = CallState.NULL
    last_code: int = 0
    last_reason: str = ""
    codec: str = ""
    clock_rate: int = 0
    channels: int = 0
    on_hold: bool = False
    muted: bool = False
    started_at: float = field(default_factory=time.time)
    connected_at: float | None = None
    ended_at: float | None = None
    # FAS (False Answer Supervision) detection. Updated by FasInferenceWorker
    # via sip_events().call_fas_verdict.
    # verdict: "" before first analysis, then a FAS verdict string such as
    #   ANALYZING, INCONCLUSIVE, HUMAN_LIKELY, MACHINE_OR_VOICEMAIL,
    #   IVR_OR_ANNOUNCEMENT, SUSPICIOUS, PROBABLE_FAS, CONFIRMED_FAS.
    # Older persisted/UI paths may still carry LIKELY_REAL / LIKELY_FAS.
    fas_verdict: str = ""
    fas_confidence: float = 0.0      # 0.0..1.0
    fas_reasons: str = ""
    fas_updated_at: float | None = None

    @property
    def duration_s(self) -> float:
        if self.connected_at is None:
            return 0.0
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, end - self.connected_at)

    @property
    def is_active(self) -> bool:
        return self.state not in (CallState.NULL, CallState.DISCONNECTED)


class CallManager(QObject):
    """Single registry of every call across every account."""

    call_added = Signal(int)              # call_id
    call_updated = Signal(int)            # call_id (state/media/hold/mute changed)
    call_removed = Signal(int)            # call_id (DISCONNECTED, cleaned up)

    def __init__(self) -> None:
        super().__init__()
        self._calls: dict[int, CallRecord] = {}

    # ------------------------------------------------------------------
    # Mutators (called from MainWindow event handlers — Qt main thread)
    # ------------------------------------------------------------------
    def register(self, rec: CallRecord) -> None:
        if rec.call_id in self._calls:
            log.warning("CallManager.register: duplicate call_id %s", rec.call_id)
            return
        self._calls[rec.call_id] = rec
        self.call_added.emit(rec.call_id)

    def update_state(self, call_id: int, new_state: CallState, code: int = 0, reason: str = "") -> bool:
        rec = self._calls.get(call_id)
        if rec is None:
            log.warning("update_state for unknown call_id %s", call_id)
            return False
        if not is_legal_transition(rec.state, new_state):
            log.warning("Dropped illegal transition %s -> %s (call %s)", rec.state, new_state, call_id)
            return False
        prev = rec.state
        rec.state = new_state
        rec.last_code = code
        rec.last_reason = reason
        # Hold/resume reset the on_hold flag implicitly
        if new_state == CallState.HELD:
            rec.on_hold = True
        elif new_state == CallState.CONFIRMED and prev == CallState.HELD:
            rec.on_hold = False
        if new_state == CallState.CONFIRMED and rec.connected_at is None:
            rec.connected_at = time.time()
        if new_state == CallState.DISCONNECTED:
            rec.ended_at = time.time()
            self.call_updated.emit(call_id)
            # Hold the record one tick so observers can read final state, then drop
            self._calls.pop(call_id, None)
            self.call_removed.emit(call_id)
            return True
        self.call_updated.emit(call_id)
        return True

    def update_media(self, call_id: int, codec: str, clock_rate: int, channels: int) -> None:
        rec = self._calls.get(call_id)
        if rec is None:
            return
        rec.codec = codec
        rec.clock_rate = clock_rate
        rec.channels = channels
        self.call_updated.emit(call_id)

    def update_remote(self, call_id: int, remote_uri: str) -> None:
        rec = self._calls.get(call_id)
        if rec is None or not remote_uri:
            return
        if rec.remote_uri == remote_uri:
            return
        rec.remote_uri = remote_uri
        self.call_updated.emit(call_id)

    def set_mute(self, call_id: int, muted: bool) -> None:
        rec = self._calls.get(call_id)
        if rec is None:
            return
        rec.muted = muted
        self.call_updated.emit(call_id)

    def update_fas(self, call_id: int, verdict: str, confidence: float, reasons: str) -> None:
        rec = self._calls.get(call_id)
        if rec is None:
            return
        rec.fas_verdict = verdict
        rec.fas_confidence = float(confidence)
        rec.fas_reasons = reasons
        rec.fas_updated_at = time.time()
        self.call_updated.emit(call_id)

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------
    def get(self, call_id: int) -> CallRecord | None:
        return self._calls.get(call_id)

    def all(self) -> list[CallRecord]:
        return list(self._calls.values())

    def active(self) -> list[CallRecord]:
        return [r for r in self._calls.values() if r.is_active]

    def first_active(self) -> CallRecord | None:
        for r in self._calls.values():
            if r.is_active:
                return r
        return None


_manager: CallManager | None = None


def call_manager() -> CallManager:
    """Process-wide singleton. Lazily constructed so tests can avoid Qt."""
    global _manager
    if _manager is None:
        _manager = CallManager()
    return _manager
