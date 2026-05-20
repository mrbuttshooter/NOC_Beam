"""Rules-engine truth table.

Each test feeds a controlled bag of signals to synthesise() and asserts
the expected verdict. No models / audio involved -- this is pure logic.
"""
from __future__ import annotations

from noc_beam.audio.fas_features import FeatureBundle
from noc_beam.audio.fas_fingerprint import FingerprintMemory
from noc_beam.audio.fas_rules import synthesise


def _features(
    *,
    silence: float = 0.0,
    ringback: float = 0.0,
    ringback_freq: float = 0.0,
    stability: float = 0.5,
    runs: int = 4,
    rms_db: float = -25.0,
) -> FeatureBundle:
    return FeatureBundle(
        silence_score=silence,
        ringback_score=ringback,
        ringback_freq_hz=ringback_freq,
        energy_stability=stability,
        speech_run_count=runs,
        rms_db=rms_db,
    )


def test_real_call_with_speech_classifies_as_real():
    v = synthesise(
        features=_features(runs=8, stability=0.3, silence=0.05),
        silero_speech_prob=0.85,
        aasist_spoof_prob=0.10,
        panns={"speech": 0.92, "music": 0.05, "ringing": 0.0, "silence": 0.0, "noise": 0.05},
        fingerprint_sim=0.30,
    )
    assert v.verdict == "HUMAN_LIKELY"
    assert v.confidence >= 0.5
    assert any(e.kind == "varied_speech_pattern" for e in v.evidence)


def test_early_silence_after_answer_stays_inconclusive():
    v = synthesise(
        features=_features(silence=0.90, runs=0),
        silero_speech_prob=0.05,
        aasist_spoof_prob=None,
        panns=None,
        fingerprint_sim=0.0,
        analyzed_seconds=4.0,
    )
    assert v.verdict == "INCONCLUSIVE"


def test_sustained_silence_after_answer_is_suspicious():
    v = synthesise(
        features=_features(silence=0.90, runs=0),
        silero_speech_prob=0.05,
        aasist_spoof_prob=None,
        panns=None,
        fingerprint_sim=0.0,
        analyzed_seconds=8.0,
        sustained_silence_seconds=8.0,
    )
    assert v.verdict == "SUSPICIOUS"
    assert any("silence" in r.lower() for r in v.reasons)


def test_ringback_after_answer_locks_fas():
    v = synthesise(
        features=_features(ringback=0.85, ringback_freq=440.0, runs=0),
        silero_speech_prob=0.05,
        aasist_spoof_prob=None,
        panns=None,
        fingerprint_sim=0.0,
    )
    assert v.verdict == "CONFIRMED_FAS"
    assert any("ringback" in r.lower() for r in v.reasons)
    assert any(e.kind == "post_answer_call_progress" and e.sticky for e in v.evidence)
    assert v.confidence >= 0.80


def test_fingerprint_reuse_alone_marks_suspicious():
    v = synthesise(
        features=_features(runs=4),
        silero_speech_prob=0.50,
        aasist_spoof_prob=None,
        panns=None,
        fingerprint_sim=0.95,
    )
    # Score: fp(3) + vad_high(-2 if >=0.6) -> here 0.5 < 0.6 so no VAD penalty
    # Net +3 -> SUSPICIOUS at balanced
    assert v.verdict in ("SUSPICIOUS", "LIKELY_FAS")


def test_fingerprint_plus_silence_plus_aasist_is_fas():
    v = synthesise(
        features=_features(silence=0.80, runs=0),
        silero_speech_prob=0.10,
        aasist_spoof_prob=0.85,
        panns=None,
        fingerprint_sim=0.97,
        analyzed_seconds=8.0,
        sustained_silence_seconds=8.0,
    )
    # +3 fp +2 silence +2 aasist = +7 -> PROBABLE_FAS
    assert v.verdict == "PROBABLE_FAS"
    assert v.confidence >= 0.7
    assert any(e.kind == "audio_reuse" and e.sticky for e in v.evidence)


def test_no_signals_is_inconclusive():
    v = synthesise(
        features=_features(),
        silero_speech_prob=None,
        aasist_spoof_prob=None,
        panns=None,
        fingerprint_sim=0.0,
    )
    assert v.verdict == "INCONCLUSIVE"


def test_aggressive_preset_fires_easier_than_balanced():
    args = dict(
        features=_features(silence=0.75, runs=0),
        silero_speech_prob=None,
        aasist_spoof_prob=None,
        panns=None,
        fingerprint_sim=0.0,
        analyzed_seconds=8.0,
        sustained_silence_seconds=8.0,
    )
    balanced = synthesise(**args, sensitivity="balanced")
    aggressive = synthesise(**args, sensitivity="aggressive")
    conservative = synthesise(**args, sensitivity="conservative")
    # Same input; aggressive should be at-least-as-severe as balanced,
    # conservative at-most-as-severe.
    severity = {
        "HUMAN_LIKELY": 0,
        "LIKELY_REAL": 0,
        "INCONCLUSIVE": 1,
        "MACHINE_OR_VOICEMAIL": 1,
        "IVR_OR_ANNOUNCEMENT": 1,
        "SUSPICIOUS": 2,
        "LIKELY_FAS": 3,
        "PROBABLE_FAS": 3,
        "CONFIRMED_FAS": 4,
    }
    assert severity[aggressive.verdict] >= severity[balanced.verdict]
    assert severity[conservative.verdict] <= severity[balanced.verdict]


def test_panns_music_with_no_speech_adds_moh():
    v = synthesise(
        features=_features(stability=0.85, runs=0, silence=0.10),
        silero_speech_prob=0.20,
        aasist_spoof_prob=None,
        panns={"speech": 0.10, "music": 0.75, "ringing": 0.0, "silence": 0.0, "noise": 0.05},
        fingerprint_sim=0.0,
    )
    assert any("music" in r.lower() for r in v.reasons)


def test_recording_signal_is_machine_not_fas_by_itself():
    v = synthesise(
        features=_features(stability=0.85, runs=1, silence=0.10),
        silero_speech_prob=0.20,
        aasist_spoof_prob=0.88,
        panns=None,
        fingerprint_sim=0.0,
    )
    assert v.verdict == "MACHINE_OR_VOICEMAIL"
    assert any(e.kind == "recorded_or_synthetic_audio" for e in v.evidence)


def test_fingerprint_memory_scopes_by_supplier_when_present():
    memory = FingerprintMemory()
    fp = "10101010" * 8
    memory.add(fp, call_id=1, account_id="acc", supplier="080")

    same_supplier, _ = memory.match(fp, call_id=2, account_id="acc", supplier="080")
    other_supplier, _ = memory.match(fp, call_id=3, account_id="acc", supplier="207")

    assert same_supplier >= 0.90
    assert other_supplier == 0.0
