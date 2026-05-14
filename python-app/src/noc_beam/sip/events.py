"""Qt-side event bus for SIP events.

PJSIP fires callbacks on its own threads. We must NEVER touch Qt widgets from
those threads — the rule is to convert each callback to a Qt signal emission,
which Qt then delivers on the main thread.

The SipEvents object is the single global instance every SIP wrapper emits
into. Widgets connect to its signals.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class SipEvents(QObject):
    # --- registration ---
    # account_id, code, reason
    registration_changed = Signal(str, int, str)

    # --- calls ---
    # account_id, call_id, remote_uri, is_incoming
    call_incoming = Signal(str, int, str, bool)
    # account_id, call_id, state_name, last_status_code, last_status_reason
    call_state_changed = Signal(str, int, str, int, str)
    # call_id, codec_name, clock_rate, channels
    call_media_active = Signal(int, str, int, int)
    # call_id
    call_ended = Signal(int)
    # call_id, digit
    call_dtmf = Signal(int, str)

    # --- transport / endpoint ---
    endpoint_started = Signal()
    endpoint_stopped = Signal()
    endpoint_error = Signal(str)

    # --- raw SIP messages (for trace viewer) ---
    # timestamp, direction ("RX"/"TX"), peer, message
    sip_message = Signal(float, str, str, str)

    # --- live media quality ---
    # call_id, mos (1.0..4.5), packet_loss_pct, jitter_ms, rtt_ms
    call_quality = Signal(int, float, float, float, float)

    # --- generic log line (pjsip log_cb) ---
    log_line = Signal(int, str)


_events: SipEvents | None = None


def sip_events() -> SipEvents:
    global _events
    if _events is None:
        _events = SipEvents()
    return _events
