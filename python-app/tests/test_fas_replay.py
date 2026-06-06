"""Offline replay harness tests.

Model-agnostic: when the ONNX models aren't bundled (CI), the wrappers
return None and the rules engine still produces a verdict, so these run
everywhere. They assert structure + determinism, not specific verdicts
(which depend on the models).
"""
from __future__ import annotations

import wave

import numpy as np

from noc_beam.audio.fas_replay import (
    ReplayResult,
    load_wav_16k_mono,
    replay_samples,
    replay_wav,
)
from noc_beam.audio.fas_tap import FAS_SAMPLE_RATE

SR = FAS_SAMPLE_RATE


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SR * seconds), dtype=np.int16)


def _tone(freq: float, seconds: float, amp_db: float = -10.0) -> np.ndarray:
    n = int(SR * seconds)
    t = np.arange(n) / SR
    amp = 10.0 ** (amp_db / 20.0)
    return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)


def test_replay_returns_scheduled_ticks():
    result = replay_samples(_silence(14.0))
    assert isinstance(result, ReplayResult)
    # Schedule hits 4, 8, 13 within a 14s clip.
    tick_times = [round(t.t) for t in result.ticks]
    assert 4 in tick_times and 8 in tick_times and 13 in tick_times
    assert result.final_verdict  # some verdict string


def test_replay_is_deterministic():
    clip = _tone(440.0, 13.0)
    a = replay_samples(clip)
    b = replay_samples(clip)
    assert a.final_verdict == b.final_verdict
    assert [t.verdict for t in a.ticks] == [t.verdict for t in b.ticks]


def test_replay_short_clip_warms_up():
    result = replay_samples(_silence(1.0))  # below the 2s minimum
    assert result.final_verdict == "ANALYZING"


def test_wav_roundtrip_and_replay(tmp_path):
    path = tmp_path / "clip.wav"
    clip = _tone(440.0, 5.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(clip.tobytes())
    loaded = load_wav_16k_mono(path)
    assert loaded.dtype == np.int16
    assert abs(loaded.size - clip.size) <= 1
    result = replay_wav(path)
    assert result.duration_s > 4.0
    assert result.as_dict()["path"].endswith("clip.wav")


def test_wav_resampled_from_8k(tmp_path):
    # An 8 kHz file (real telephone rate) must be resampled to 16 kHz on load.
    path = tmp_path / "nb.wav"
    n = 8000 * 4
    clip = (np.sin(2 * np.pi * 440 * np.arange(n) / 8000) * 0.3 * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(clip.tobytes())
    loaded = load_wav_16k_mono(path)
    # ~4s at 16k after resample.
    assert abs(loaded.size - 16000 * 4) < 16000 * 0.1
