"""Sanity checks for the R-factor → MOS conversion + loss/jitter shape."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtCore")

from noc_beam.sip.quality import estimate_mos, r_factor_to_mos  # noqa: E402


def test_r_factor_clamps() -> None:
    assert r_factor_to_mos(-10) == 1.0
    assert r_factor_to_mos(0) == 1.0
    assert r_factor_to_mos(200) == 4.5


def test_perfect_link_is_excellent() -> None:
    mos = estimate_mos(packet_loss_pct=0.0, jitter_ms=0.0, rtt_ms=0.0)
    assert mos > 4.0


def test_packet_loss_degrades_mos() -> None:
    clean = estimate_mos(0.0, 5.0, 50.0)
    lossy = estimate_mos(5.0, 5.0, 50.0)
    assert lossy < clean
    # 5% loss should not be "excellent"
    assert lossy < 4.0


def test_long_one_way_delay_degrades_mos() -> None:
    short = estimate_mos(0.0, 10.0, 100.0)
    long = estimate_mos(0.0, 10.0, 800.0)
    assert long < short


def test_extreme_loss_pegs_low() -> None:
    mos = estimate_mos(packet_loss_pct=50.0, jitter_ms=200.0, rtt_ms=2000.0)
    assert mos < 2.0
