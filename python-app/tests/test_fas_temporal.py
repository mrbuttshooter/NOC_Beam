"""Temporal robustness fixes, tested through the shared scoring helpers.

These exercise the fas_worker policy (k-of-n AASIST voting, minimum-voiced
budget, escalation hysteresis, monotonic vs de-escalation) WITHOUT a
QThread, ONNX models, or a live call -- the helpers are pure functions over
a _CallScoreState.
"""
from __future__ import annotations

from noc_beam.audio.fas_features import FeatureBundle
from noc_beam.audio.fas_rules import AASIST_SPOOF_THRESHOLD
from noc_beam.audio.fas_worker import (
    _CallScoreState,
    apply_surface_policy,
    evaluate_snapshot,
    vote_aasist,
)


def _features(*, silence=0.0, ringback=0.0, stability=0.5, runs=4, rms_db=-25.0):
    return FeatureBundle(
        silence_score=silence,
        ringback_score=ringback,
        ringback_freq_hz=0.0,
        energy_stability=stability,
        speech_run_count=runs,
        rms_db=rms_db,
    )


# ---- k-of-n AASIST voting ------------------------------------------------
def test_single_high_aasist_read_does_not_fire():
    state = _CallScoreState()
    voted = vote_aasist(state, 0.95)
    assert voted < AASIST_SPOOF_THRESHOLD  # one frame can't latch


def test_two_high_reads_corroborate_and_fire():
    state = _CallScoreState()
    vote_aasist(state, 0.95)
    voted = vote_aasist(state, 0.92)
    assert voted >= AASIST_SPOOF_THRESHOLD  # 2-of-2 -> median fires


def test_one_fluke_among_reads_does_not_fire():
    state = _CallScoreState()
    vote_aasist(state, 0.95)          # fluke spike
    voted = vote_aasist(state, 0.10)  # real audio is clean
    assert voted < AASIST_SPOOF_THRESHOLD  # 1-of-2 -> suppressed


# ---- minimum-voiced budget ----------------------------------------------
def _high_fas_snapshot(state, *, window_seconds, is_first):
    return evaluate_snapshot(
        state,
        features=_features(silence=0.10, runs=0, stability=0.85),
        silero_p=0.05,
        aasist_p=0.95,  # already-voted, fires
        panns_out={"speech": 0.05, "music": 0.80, "ringing": 0.60,
                   "silence": 0.0, "noise": 0.05},
        fp_sim=0.0,
        fingerprint_match=None,
        analyzed_seconds=8.0,
        sensitivity="aggressive",
        window_seconds=window_seconds,
        elapsed_since_last=window_seconds,
        is_first_score=is_first,
    )


def test_high_verdict_capped_until_enough_voice():
    state = _CallScoreState()
    # First tick: only 1s of voiced audio -> capped at SUSPICIOUS.
    verdict, _, reasons = _high_fas_snapshot(state, window_seconds=1.0, is_first=True)
    assert verdict == "SUSPICIOUS"
    assert "awaiting more voice" in reasons


def test_high_verdict_allowed_once_voice_budget_met():
    state = _CallScoreState()
    _high_fas_snapshot(state, window_seconds=1.0, is_first=True)      # voiced=1.0
    verdict, _, _ = _high_fas_snapshot(state, window_seconds=2.0, is_first=False)  # voiced=3.0
    assert verdict == "PROBABLE_FAS"


# ---- escalation hysteresis ----------------------------------------------
def test_nondeterministic_escalation_needs_two_ticks():
    state = _CallScoreState()
    v1, _, _ = apply_surface_policy(state, "PROBABLE_FAS", 0.80, "x")
    assert v1 != "PROBABLE_FAS"          # first tick held
    v2, _, _ = apply_surface_policy(state, "PROBABLE_FAS", 0.80, "x")
    assert v2 == "PROBABLE_FAS"          # confirmed on second


def test_deterministic_positive_escalates_immediately():
    state = _CallScoreState()
    state.deterministic_positive = True
    v, _, _ = apply_surface_policy(state, "CONFIRMED_FAS", 0.90, "ringback")
    assert v == "CONFIRMED_FAS"


# ---- monotonic default vs opt-in de-escalation --------------------------
def _commit_probable(state):
    apply_surface_policy(state, "PROBABLE_FAS", 0.80, "x")
    apply_surface_policy(state, "PROBABLE_FAS", 0.80, "x")  # now committed


def test_default_never_downgrades():
    state = _CallScoreState()
    _commit_probable(state)
    for _ in range(5):
        v, _, _ = apply_surface_policy(state, "INCONCLUSIVE", 0.10, "y",
                                       allow_deescalation=False)
        assert v == "PROBABLE_FAS"       # monotonic lock holds


def test_deescalation_steps_down_after_sustained_low():
    state = _CallScoreState()
    _commit_probable(state)
    last = "PROBABLE_FAS"
    for _ in range(4):
        last, _, _ = apply_surface_policy(state, "INCONCLUSIVE", 0.10, "y",
                                          allow_deescalation=True)
    assert last == "INCONCLUSIVE"        # eventually downgrades when enabled
