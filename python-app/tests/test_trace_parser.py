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


# Verbatim preambles written by the bundled PJSIP 2.14.1. The old patterns
# required "from"/"to" right after "bytes" and matched none of these, so
# every trace message on the real engine came out as direction "?".
PJSIP_214_TX = ("21:43:44.334   pjsua_core.c  ....TX 1351 bytes Request msg "
                "INVITE/cseq=23508 (tdta000002841EFBFF18) to UDP 127.0.0.1:5098:")
PJSIP_214_RX = ("21:43:44.355   pjsua_core.c  .RX 312 bytes Response msg "
                "100/INVITE/cseq=23508 (rdata000002841F2D6FE8) from UDP 127.0.0.1:5098:")


def test_pjsip_214_preambles_parsed_with_transport_and_address() -> None:
    from noc_beam.sip.trace import _preamble_peer

    tx = _DIR_TX.search(PJSIP_214_TX)
    rx = _DIR_RX.search(PJSIP_214_RX)
    assert tx and rx
    assert _preamble_peer(tx) == "UDP 127.0.0.1:5098"
    assert _preamble_peer(rx) == "UDP 127.0.0.1:5098"
    assert not _DIR_RX.search(PJSIP_214_TX)
    assert not _DIR_TX.search(PJSIP_214_RX)


def test_ipv6_and_tls_peers() -> None:
    from noc_beam.sip.trace import _preamble_peer

    line = "10:00:00.000 pjsua_core.c .TX 900 bytes Request msg BYE/cseq=2 (tdta01) to TLS [2001:db8::1]:5061:"
    m = _DIR_TX.search(line)
    assert m and _preamble_peer(m) == "TLS [2001:db8::1]:5061"


def test_trace_writer_tags_real_214_stream_with_directions() -> None:
    """End to end: the writer turns a 2.14 log stream into RX/TX messages."""
    from noc_beam.sip.events import sip_events
    from noc_beam.sip.trace import TraceLogWriter

    got: list[tuple[str, str, str]] = []

    def _grab(ts, direction, peer, body):
        got.append((direction, peer, body.splitlines()[0]))

    sip_events().sip_message.connect(_grab)
    try:
        w = TraceLogWriter()
        stream = [
            PJSIP_214_TX,
            "INVITE sip:callee@127.0.0.1:5098 SIP/2.0",
            "Call-ID: abc",
            "CSeq: 23508 INVITE",
            "--end msg--",
            PJSIP_214_RX,
            "SIP/2.0 100 Trying",
            "Call-ID: abc",
            "--end msg--",
            "21:43:44.400   pjsua_call.c  .Call 0 state changed to CALLING",
        ]
        for line in stream:
            with w._lock:
                w._consume(line)
    finally:
        sip_events().sip_message.disconnect(_grab)
    assert [(d, p) for d, p, _ in got] == [
        ("TX", "UDP 127.0.0.1:5098"),
        ("RX", "UDP 127.0.0.1:5098"),
    ]
    assert got[0][2].startswith("INVITE sip:")
    assert got[1][2] == "SIP/2.0 100 Trying"
