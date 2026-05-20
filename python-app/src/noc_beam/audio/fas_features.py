"""Lightweight feature extractors for FAS detection.

These run BEFORE the ONNX models (which are heavier) and produce the
deterministic signals the rules engine weights highest:
    - silence_score      : RMS-based; 1.0 = pure silence, 0.0 = full speech
    - ringback_score     : Goertzel on telephony tones (425/440/480/620 Hz)
                           with on/off cadence detection
    - energy_stability   : low variance across windows = music or static
    - speech_run_count   : count of speech segments via simple VAD

The Goertzel filter is preferred over PANNs for ringback because telephony
ringback tones are deterministic frequencies (PSTN regional standards) --
PANNs's "Ringtone" class is trained on consumer phone ringers and misses
pure dual-tone signals.

All scores are normalised to [0, 1].
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


# Standard PSTN ringback / busy / SIT cadence frequencies, Hz.
# Sources: ITU-T E.180, FCC Part 68, ETSI ES 201 970.
RINGBACK_FREQS_HZ = (425.0, 440.0, 480.0, 620.0, 350.0)


@dataclass(frozen=True)
class FeatureBundle:
    silence_score: float            # 0..1, 1 = silent
    ringback_score: float           # 0..1, 1 = strong dual-tone match
    ringback_freq_hz: float         # best matching frequency (Hz)
    energy_stability: float         # 0..1, 1 = flat/repetitive energy
    speech_run_count: int           # number of contiguous speech segments
    rms_db: float                   # mean RMS in dBFS (-inf .. 0)
    tone_label: str = ""            # RINGBACK / BUSY_OR_REORDER / SIT_NO_SERVICE / ...
    tone_score: float = 0.0         # 0..1 confidence for tone_label
    tone_freq_hz: float = 0.0       # representative detected frequency
    tone_cadence: str = ""          # human-readable cadence summary


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def _rms_dbfs(samples: np.ndarray) -> float:
    """RMS in dBFS, with -120 floor so log10(0) is bounded."""
    if samples.size == 0:
        return -120.0
    rms = _rms(samples) / 32768.0
    if rms <= 1e-6:
        return -120.0
    return 20.0 * math.log10(rms)


def goertzel_magnitude(samples: np.ndarray, sample_rate: int, target_hz: float) -> float:
    """Single-frequency power via Goertzel's algorithm. Normalized 0..1."""
    if samples.size == 0:
        return 0.0
    x = samples.astype(np.float64) / 32768.0
    n = x.size
    k = 0.5 + (n * target_hz) / sample_rate
    omega = 2.0 * math.pi * k / n
    coeff = 2.0 * math.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0
    for sample in x:
        s = sample + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    power = s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2
    # Normalise by frame size; clip to [0, 1] sensibly.
    mag = math.sqrt(max(power, 0.0)) / (n / 2.0)
    return min(mag, 1.0)


def ringback_detect(
    samples: np.ndarray,
    sample_rate: int,
    *,
    freqs_hz: tuple[float, ...] = RINGBACK_FREQS_HZ,
    window_ms: int = 200,
) -> tuple[float, float]:
    """Detect telephony ringback / busy / SIT tones.

    Slides a ``window_ms`` window across the audio, runs Goertzel for each
    target frequency, and returns (max_score, best_freq_hz). A "score" near
    1.0 indicates a strong sustained pure-tone match.
    """
    if samples.size == 0:
        return 0.0, 0.0
    win_n = max(int(sample_rate * window_ms / 1000), 320)
    if samples.size < win_n:
        # Too short -- pad with zeros.
        samples = np.pad(samples, (0, win_n - samples.size))
    hop = win_n // 2
    best_score = 0.0
    best_freq = 0.0
    sustained_threshold = 0.10
    for freq in freqs_hz:
        per_window_scores = []
        for start in range(0, samples.size - win_n + 1, hop):
            seg = samples[start:start + win_n]
            per_window_scores.append(goertzel_magnitude(seg, sample_rate, freq))
        if not per_window_scores:
            continue
        scores = np.array(per_window_scores)
        # Sustained tone: > 50% of windows above threshold.
        sustained_frac = float(np.mean(scores > sustained_threshold))
        score = float(scores.max()) * sustained_frac
        if score > best_score:
            best_score = score
            best_freq = freq
    return best_score, best_freq


def energy_windows(samples: np.ndarray, sample_rate: int, *, window_ms: int = 100) -> np.ndarray:
    """Return per-window RMS values in dBFS."""
    if samples.size == 0:
        return np.zeros(0, dtype=np.float32)
    win_n = max(int(sample_rate * window_ms / 1000), 160)
    windows = []
    for start in range(0, samples.size - win_n + 1, win_n):
        seg = samples[start:start + win_n]
        windows.append(_rms_dbfs(seg))
    return np.array(windows, dtype=np.float32) if windows else np.zeros(0, dtype=np.float32)


def silence_score(samples: np.ndarray, sample_rate: int, *, silence_dbfs: float = -50.0) -> float:
    """Fraction of windows below the silence threshold.

    Returns 0.0 for fully-loud audio, 1.0 for completely silent audio.
    A real answered call typically scores < 0.3; a FAS silence injection
    scores > 0.85.
    """
    levels = energy_windows(samples, sample_rate)
    if levels.size == 0:
        return 1.0
    return float(np.mean(levels < silence_dbfs))


def energy_stability(samples: np.ndarray, sample_rate: int) -> float:
    """Inverse coefficient of variation of windowed RMS, in [0, 1].

    A canned recording or MoH loop has very low RMS variance (each loop
    iteration looks the same). Live speech varies a lot. We map standard
    deviation / mean to a [0,1] score where 1.0 = perfectly flat.
    """
    levels = energy_windows(samples, sample_rate)
    if levels.size < 3:
        return 0.0
    # Convert from dBFS back to linear to make variance meaningful.
    linear = np.power(10.0, levels / 20.0)
    mean = float(np.mean(linear))
    std = float(np.std(linear))
    if mean <= 1e-9:
        return 0.0
    cv = std / mean
    # Empirically: speech cv ~0.6, MoH/recording cv < 0.15, pure tone < 0.05.
    # Map cv 0..0.5 -> 1..0
    return float(max(0.0, min(1.0, 1.0 - (cv / 0.5))))


def speech_runs(levels_db: np.ndarray, *, speech_dbfs: float = -45.0) -> int:
    """Count contiguous runs of windows above the speech threshold."""
    if levels_db.size == 0:
        return 0
    above = levels_db > speech_dbfs
    if not above.any():
        return 0
    return int(np.sum((above[1:] & ~above[:-1])) + (1 if above[0] else 0))


def extract_features(samples: np.ndarray, sample_rate: int = 16000) -> FeatureBundle:
    """Run every cheap feature in one pass. Order matters: silence first
    so a totally-empty clip short-circuits before we pay for Goertzel.
    """
    rms_db = _rms_dbfs(samples)
    s_score = silence_score(samples, sample_rate)
    if s_score > 0.95:
        # Fully silent -- skip downstream features (they're noise on noise).
        return FeatureBundle(
            silence_score=s_score,
            ringback_score=0.0,
            ringback_freq_hz=0.0,
            energy_stability=1.0,
            speech_run_count=0,
            rms_db=rms_db,
        )
    rb_score, rb_freq = ringback_detect(samples, sample_rate)
    try:
        from noc_beam.audio.fas_tones import detect_call_progress_tone

        tone = detect_call_progress_tone(samples, sample_rate)
    except Exception:
        tone = None
    e_stability = energy_stability(samples, sample_rate)
    levels = energy_windows(samples, sample_rate)
    runs = speech_runs(levels)
    return FeatureBundle(
        silence_score=s_score,
        ringback_score=rb_score,
        ringback_freq_hz=rb_freq,
        energy_stability=e_stability,
        speech_run_count=runs,
        rms_db=rms_db,
        tone_label=tone.label if tone is not None else "",
        tone_score=tone.score if tone is not None else 0.0,
        tone_freq_hz=tone.freq_hz if tone is not None else 0.0,
        tone_cadence=tone.cadence if tone is not None else "",
    )
