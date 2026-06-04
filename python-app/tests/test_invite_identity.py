from __future__ import annotations

from noc_beam.config.store import AccountConfig
from noc_beam.sip.endpoint import SipEndpoint


def test_invite_local_uri_carries_display_name_as_from_display() -> None:
    cfg = AccountConfig(
        id="a",
        display_name="96171488860",
        username="U080",
        domain="208.87.170.99",
    )

    assert SipEndpoint._format_invite_local_uri(cfg) == (
        '"96171488860" <sip:U080@208.87.170.99>'
    )


def test_invite_local_uri_omits_when_no_display_name() -> None:
    cfg = AccountConfig(id="a", username="U080", domain="208.87.170.99")

    assert SipEndpoint._format_invite_local_uri(cfg) == ""


def test_invite_local_uri_keeps_transport_and_port() -> None:
    cfg = AccountConfig(
        id="a",
        display_name='961714"88860',
        username="U080",
        domain="sip.example.test",
        transport="tcp",
        port=5070,
    )

    assert SipEndpoint._format_invite_local_uri(cfg) == (
        '"96171488860" <sip:U080@sip.example.test:5070;transport=tcp>'
    )


def test_teles_invite_local_uri_udp_has_no_transport_param() -> None:
    # Teles UDP accounts are no longer force-coerced to TCP (that broke
    # in-dialog BYE/486 delivery on the Communi5 switch). UDP URIs carry
    # no transport param.
    cfg = AccountConfig(
        id="a",
        display_name="96171488860",
        username="U080",
        domain="208.87.170.99",
        switch_type="teles",
        transport="udp",
    )

    assert SipEndpoint._format_invite_local_uri(cfg) == (
        '"96171488860" <sip:U080@208.87.170.99>'
    )


def test_teles_codec_profile_matches_legacy_eyebeam_offer() -> None:
    priorities = {
        "PCMA/8000": 245,
        "PCMU/8000": 240,
        "G722/16000": 235,
        "opus/48000": 230,
        "G729/8000": 220,
        "iLBC/8000": 210,
        "speex/16000": 200,
        "speex/8000": 195,
        "GSM/8000": 190,
    }
    result = SipEndpoint._effective_codec_priorities(
        priorities,
        [AccountConfig(id="a", switch_type="teles")],
    )

    assert result["PCMU/8000"] > result["PCMA/8000"] > result["G729/8000"]
    assert result["opus/48000"] == 0
    assert result["G722/16000"] == 0
    assert result["iLBC/8000"] == 0
    assert result["speex/16000"] == 0
    assert result["speex/8000"] == 0
    assert result["GSM/8000"] == 0
