"""DTMF send path: RFC2833 must fall back to SIP INFO instead of dying.

Field logs (2026-06) showed every keypress on an IP-trunk call logging
"send_dtmf failed" because pjsua2's dialDtmf raises when the negotiated
media has no telephone-event (RFC 4733) format. send_dtmf now catches
that and falls back to SIP INFO. These tests pin that behaviour without
needing a live pjsua2 endpoint -- they invoke the unbound method against
a tiny stub `self`.
"""
from __future__ import annotations

import types

from noc_beam.sip.endpoint import SipEndpoint


class _StubCfg:
    def __init__(self, method: str) -> None:
        self.dtmf_method = method


class _Call:
    def __init__(self, *, dial_raises: bool) -> None:
        self.dial_raises = dial_raises
        self.dialed: list[str] = []

    def dialDtmf(self, digits: str) -> None:  # noqa: N802 (pjsua2 name)
        self.dialed.append(digits)
        if self.dial_raises:
            raise RuntimeError("no telephone-event negotiated")


def _stub_self():
    """A minimal object exposing only what send_dtmf touches."""
    s = types.SimpleNamespace()
    s.info_calls = []
    s._send_dtmf_info = lambda call, digits: s.info_calls.append((call, digits))
    return s


def test_rfc2833_success_does_not_fall_back() -> None:
    s = _stub_self()
    call = _Call(dial_raises=False)
    SipEndpoint.send_dtmf(s, call, "5", _StubCfg("rfc2833"))
    assert call.dialed == ["5"]
    assert s.info_calls == []  # no fallback when RFC2833 works


def test_rfc2833_failure_falls_back_to_sip_info() -> None:
    s = _stub_self()
    call = _Call(dial_raises=True)
    SipEndpoint.send_dtmf(s, call, "7", _StubCfg("rfc2833"))
    # Tried RFC2833 first...
    assert call.dialed == ["7"]
    # ...then fell back to SIP INFO instead of letting the error escape.
    assert s.info_calls == [(call, "7")]


def test_info_method_uses_sip_info_directly() -> None:
    s = _stub_self()
    call = _Call(dial_raises=False)
    SipEndpoint.send_dtmf(s, call, "9", _StubCfg("info"))
    assert s.info_calls == [(call, "9")]
    assert call.dialed == []  # never touches dialDtmf


def test_default_method_is_rfc2833_with_fallback() -> None:
    s = _stub_self()
    call = _Call(dial_raises=True)
    SipEndpoint.send_dtmf(s, call, "0", _StubCfg(""))  # empty -> rfc2833
    assert s.info_calls == [(call, "0")]
