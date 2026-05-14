"""Codec-id matcher: more-specific keys win, partial collisions are blocked."""
from __future__ import annotations

import pytest

# The matcher is a static method that doesn't need PJSIP; we still need
# PySide6 because SipEndpoint imports it transitively.
pytest.importorskip("PySide6.QtCore")

from noc_beam.sip.endpoint import SipEndpoint  # noqa: E402


def test_name_only_key_matches_any_clockrate() -> None:
    assert SipEndpoint._codec_match("opus", "opus/48000/2")
    assert SipEndpoint._codec_match("opus", "opus/24000/1")
    assert SipEndpoint._codec_match("PCMA", "PCMA/8000/1")


def test_name_and_clockrate_key_requires_both() -> None:
    assert SipEndpoint._codec_match("opus/48000", "opus/48000/2")
    assert not SipEndpoint._codec_match("opus/48000", "opus/24000/1")
    assert not SipEndpoint._codec_match("opus/48000", "PCMA/48000/1")


def test_speex_8k_does_not_match_speex_16k_when_clockrate_specified() -> None:
    assert SipEndpoint._codec_match("speex/8000", "speex/8000/1")
    assert not SipEndpoint._codec_match("speex/8000", "speex/16000/1")
    assert not SipEndpoint._codec_match("speex/16000", "speex/8000/1")


def test_case_insensitive_on_name() -> None:
    assert SipEndpoint._codec_match("OPUS", "opus/48000/2")
    assert SipEndpoint._codec_match("opus", "OPUS/48000/2")


def test_unrelated_codecs_do_not_match() -> None:
    assert not SipEndpoint._codec_match("opus", "PCMA/8000/1")
    assert SipEndpoint._codec_match("ilbc", "iLBC/8000/1")    # case-insensitive
    # Name segment must match exactly — no substring fuzz.
    assert not SipEndpoint._codec_match("op", "opus/48000/2")
