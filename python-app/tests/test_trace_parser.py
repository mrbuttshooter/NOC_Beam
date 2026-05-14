"""Smoke test for the SIP trace parser regexes."""
from __future__ import annotations

from noc_beam.sip.trace import _SIP_START, _DIR_RX, _DIR_TX


def test_sip_start_matches_methods() -> None:
    samples = [
        "INVITE sip:bob@example.com SIP/2.0",
        "REGISTER sips:registrar SIP/2.0",
        "BYE sip:x@y.com SIP/2.0",
        "SIP/2.0 200 OK",
        "SIP/2.0 401 Unauthorized",
    ]
    for s in samples:
        assert _SIP_START.search(s), s


def test_sip_start_rejects_random_lines() -> None:
    samples = [
        "Via: SIP/2.0/UDP 10.0.0.1",
        "Content-Length: 0",
        "Hello world",
    ]
    for s in samples:
        assert not _SIP_START.search(s), s


def test_direction_lines_parsed() -> None:
    rx = "20:14:55.123 sip_endpoint.c .RX 421 bytes packet from UDP 10.0.0.5:5060:"
    tx = "20:14:55.234 sip_endpoint.c TX 312 bytes packet to UDP 10.0.0.5:5060:"
    assert _DIR_RX.search(rx)
    assert _DIR_TX.search(tx)
