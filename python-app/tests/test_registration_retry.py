from __future__ import annotations

from noc_beam.sip import registration_retry


def test_register_method_not_allowed_is_not_retried() -> None:
    assert 405 in registration_retry._NO_RETRY_CODES
