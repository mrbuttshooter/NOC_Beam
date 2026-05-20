"""Smoke test for the ringtone WAV generator (no audio playback)."""
from __future__ import annotations

import wave
from pathlib import Path

from noc_beam.audio import ringer


def test_generate_ringtone_writes_valid_wav(tmp_path: Path) -> None:
    out = ringer.generate_ringtone(tmp_path / "ring.wav")
    assert out.exists()
    with wave.open(str(out), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == ringer.SAMPLE_RATE
        # ~4 seconds of audio
        frames = w.getnframes()
        assert frames == int(ringer.SAMPLE_RATE * 4.0)


def test_loop_count_int_accepts_pyside_enum_value_attribute() -> None:
    class LoopEnum:
        value = -2

    assert ringer._loop_count_int(LoopEnum()) == -2


def test_loop_count_int_accepts_pyside_enum_value_method() -> None:
    class LoopEnum:
        def value(self) -> int:
            return -2

    assert ringer._loop_count_int(LoopEnum()) == -2
