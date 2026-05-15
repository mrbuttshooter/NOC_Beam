"""Incoming-call ringtone playback.

Uses Qt's QSoundEffect (PySide6.QtMultimedia) to loop a WAV on the user's
selected ringer device. If a ringtone file isn't already present in the user
data dir we generate a classic two-tone "ring-ring" pattern with the stdlib
`wave` module — no extra dependencies, ~80 KB on disk.

Falls back to a silent no-op if QtMultimedia isn't importable (PySide6
Essentials-only environment, headless CI). The caller never has to care.
"""
from __future__ import annotations

import logging
import math
import struct
import wave
from pathlib import Path

from noc_beam.config.paths import data_dir

log = logging.getLogger(__name__)

RINGTONE_FILENAME = "default_ringtone.wav"
SAMPLE_RATE = 22050              # plenty for telephony; halves file size vs 44k


def _ringtone_path() -> Path:
    return data_dir() / RINGTONE_FILENAME


def generate_ringtone(path: Path | None = None) -> Path:
    """Write a 4-second 'ring-ring' WAV to `path` and return it.

    Pattern: 480+620 Hz two-tone (UK-style cadence) — 1 s on, 1 s off, 1 s on,
    1 s off. Total 4 s. QSoundEffect will loop the file.
    """
    target = path or _ringtone_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    duration_s = 4.0
    n_samples = int(SAMPLE_RATE * duration_s)
    amplitude = 0.35
    samples = bytearray()
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        # Two 1s tone bursts at t in [0,1) and [2,3); silence elsewhere.
        burst = (0.0 <= t < 1.0) or (2.0 <= t < 3.0)
        if burst:
            # Soft attack/release envelope to avoid clicks.
            phase = t if t < 1.0 else t - 2.0
            env = min(1.0, phase / 0.05, (1.0 - phase) / 0.05)
            env = max(0.0, env)
            value = amplitude * env * (
                math.sin(2 * math.pi * 480 * t) + math.sin(2 * math.pi * 620 * t)
            ) / 2.0
        else:
            value = 0.0
        samples += struct.pack("<h", int(value * 32767))

    with wave.open(str(target), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(samples))
    log.info("Generated default ringtone at %s", target)
    return target


def ensure_default_ringtone() -> Path:
    path = _ringtone_path()
    if not path.exists():
        return generate_ringtone(path)
    return path


class Ringer:
    """Loops the default ringtone until stopped. Safe to stop when not playing."""

    def __init__(self) -> None:
        self._effect = None
        self._available = False
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QSoundEffect

            path = ensure_default_ringtone()
            self._effect = QSoundEffect()
            self._effect.setSource(QUrl.fromLocalFile(str(path)))
            # Loop forever. PySide6 6.7+ wraps Infinite in an enum class
            # but QSoundEffect.setLoopCount expects a plain int -- use the
            # enum's .value so we work on every PySide6.
            try:
                infinite = QSoundEffect.Loop.Infinite.value  # PySide6 6.7+
            except AttributeError:
                infinite = QSoundEffect.Infinite             # PySide6 <= 6.6
            self._effect.setLoopCount(infinite)
            self._effect.setVolume(0.7)
            self._available = True
        except Exception:
            log.warning("Ringer unavailable (QtMultimedia missing?); incoming calls will be silent", exc_info=True)

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> None:
        if not self._available or self._effect is None:
            return
        if not self._effect.isPlaying():
            self._effect.play()

    def stop(self) -> None:
        if not self._available or self._effect is None:
            return
        if self._effect.isPlaying():
            self._effect.stop()
