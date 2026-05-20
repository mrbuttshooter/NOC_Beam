"""End-to-end FAS pipeline integration.

Builds synthetic audio fixtures (no PJSIP, no real call) and pushes them
through:
    extract_features  ->  synthesise  ->  verdict

Validates each FAS scenario from the design doc lands on the expected
verdict bucket. Silero / AASIST / PANNs are passed as None -- their
contributions are tested in their own unit tests; this run isolates the
deterministic feature + rules pipeline so a model unavailability never
breaks CI.
"""
from __future__ import annotations

import numpy as np

from noc_beam.audio.fas_features import extract_features
from noc_beam.audio.fas_rules import synthesise

SR = 16000


def _silence(seconds: float) -> np.ndarray:
    n = int(SR * seconds)
    return np.zeros(n, dtype=np.int16)


def _tone(freq_hz: float, seconds: float, amp_db: float = -10.0) -> np.ndarray:
    n = int(SR * seconds)
    t = np.arange(n) / SR
    amp = 10.0 ** (amp_db / 20.0)
    return (np.sin(2 * np.pi * freq_hz * t) * amp * 32767).astype(np.int16)


def _speech_like(seconds: float, *, num_segments: int = 6) -> np.ndarray:
    """Random energy bursts with gaps -- shape mimics conversation cadence."""
    n = int(SR * seconds)
    rng = np.random.default_rng(0)
    out = np.zeros(n, dtype=np.float32)
    seg_len = n // (num_segments * 2)
    pos = 0
    for i in range(num_segments):
        # Burst of band-limited noise
        burst = rng.standard_normal(seg_len).astype(np.float32) * 0.25
        out[pos:pos + seg_len] = burst
        pos += seg_len * 2  # gap
        if pos >= n:
            break
    return (out * 32767).astype(np.int16)


def test_short_pure_silence_clip_stays_inconclusive():
    clip = _silence(4.0)
    features = extract_features(clip, sample_rate=SR)
    assert features.silence_score > 0.9
    v = synthesise(
        features=features,
        silero_speech_prob=None,
        aasist_spoof_prob=None,
        panns=None,
        fingerprint_sim=0.0,
        analyzed_seconds=4.0,
    )
    assert v.verdict == "INCONCLUSIVE"


def test_sustained_pure_silence_clip_is_suspicious():
    clip = _silence(8.0)
    features = extract_features(clip, sample_rate=SR)
    assert features.silence_score > 0.9
    v = synthesise(
        features=features,
        silero_speech_prob=None,
        aasist_spoof_prob=None,
        panns=None,
        fingerprint_sim=0.0,
        analyzed_seconds=8.0,
        sustained_silence_seconds=8.0,
    )
    assert v.verdict == "SUSPICIOUS"


def test_pure_440hz_ringback_is_at_least_suspicious():
    # Sustained 440 Hz tone (3s) -- the common form of ringback-after-200
    # FAS where the supplier just plays a tone forever. The on/off cadenced
    # variant is tested separately because Goertzel sustained-fraction
    # interacts with cadence in non-obvious ways.
    clip = _tone(440.0, 3.0)
    features = extract_features(clip, sample_rate=SR)
    # Goertzel score on sustained pure tone empirically lands around
    # 0.20 with our normalisation; the rules engine threshold is 0.15.
    assert features.ringback_score > 0.15
    assert 400 < features.ringback_freq_hz < 500
    v = synthesise(
        features=features,
        silero_speech_prob=None,
        aasist_spoof_prob=None,
        panns=None,
        fingerprint_sim=0.0,
    )
    assert v.verdict == "CONFIRMED_FAS"
    assert any(e.kind == "post_answer_call_progress" for e in v.evidence)


def test_realistic_speech_with_high_vad_is_real():
    clip = _speech_like(4.0, num_segments=8)
    features = extract_features(clip, sample_rate=SR)
    v = synthesise(
        features=features,
        silero_speech_prob=0.85,  # simulate Silero firing
        aasist_spoof_prob=0.05,
        panns=None,
        fingerprint_sim=0.10,
    )
    # Expect at minimum NOT to be a FAS verdict; INCONCLUSIVE or
    # HUMAN_LIKELY both pass for the synthetic noise bursts.
    assert v.verdict in ("INCONCLUSIVE", "LIKELY_REAL", "HUMAN_LIKELY")


def test_fingerprint_reuse_dominates_a_borderline_call():
    # A call that's mostly speech-like but is the EXACT same audio as a
    # previously-seen call. Fingerprint match alone should escalate it.
    clip = _speech_like(4.0)
    features = extract_features(clip, sample_rate=SR)
    v = synthesise(
        features=features,
        silero_speech_prob=0.40,    # ambiguous
        aasist_spoof_prob=None,
        panns=None,
        fingerprint_sim=0.96,        # strong match
    )
    assert v.verdict in ("SUSPICIOUS", "LIKELY_FAS")
    # Reason text should reference the FP match.
    assert any("previous call" in r.lower() for r in v.reasons)


def test_combined_silence_plus_aasist_plus_fp_is_fas():
    clip = _silence(3.0)
    features = extract_features(clip, sample_rate=SR)
    v = synthesise(
        features=features,
        silero_speech_prob=0.05,
        aasist_spoof_prob=0.92,
        panns={"speech": 0.05, "music": 0.10, "ringing": 0.0, "silence": 0.80, "noise": 0.05},
        fingerprint_sim=0.94,
        analyzed_seconds=8.0,
        sustained_silence_seconds=8.0,
    )
    # +3 (fp) +2 (silence) +2 (aasist) -2 (low VAD doesn't count -- silero_p=0.05 < 0.6)
    # = +7 -> PROBABLE_FAS
    assert v.verdict == "PROBABLE_FAS"
    assert v.confidence >= 0.7
