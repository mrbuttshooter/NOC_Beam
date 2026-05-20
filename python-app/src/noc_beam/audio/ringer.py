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


def _loop_count_int(value: object) -> int:
    """Return a plain int for PySide enum/int loop count values."""
    raw = getattr(value, "value", value)
    if callable(raw):
        raw = raw()
    return int(raw)


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
            # Loop forever. Some PySide6 builds expose Infinite as an
            # enum object even though setLoopCount() accepts only int.
            infinite = getattr(QSoundEffect, "Infinite", None)
            if infinite is None:
                infinite = QSoundEffect.Loop.Infinite
            self._effect.setLoopCount(_loop_count_int(infinite))
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


# ---------------------------------------------------------------------------
# Failure tones -- play once when a call ends with a SIP failure code so the
# operator knows by ear without watching the screen. Three canonical PSTN
# patterns cover all SIP failure classes:
#
#   BUSY     -- 480 + 620 Hz, 0.5 s on / 0.5 s off, 2 cycles (~2 s)
#               -> 486 Busy Here, 600 Busy Everywhere
#   REORDER  -- 480 + 620 Hz, 0.25 s on / 0.25 s off, 4 cycles (~2 s)
#               (a.k.a. fast busy / congestion)
#               -> 408 Timeout, 500/502/503/504 server errors
#   REJECT   -- descending 660 -> 440 Hz sweep, ~0.8 s
#               -> 487 Cancelled, 603 Declined, 480 Unavailable
#
# Auth-required codes (401 / 407) play NO tone -- they're a setup error,
# not a real call failure, and the busy-tone would be misleading.
#
# WAVs are generated on first run with the same stdlib `wave` module as
# the ringtone; each ~30-50 KB.
# ---------------------------------------------------------------------------

BUSY_TONE_FILENAME = "tone_busy.wav"
REORDER_TONE_FILENAME = "tone_reorder.wav"
REJECT_TONE_FILENAME = "tone_reject.wav"


def _busy_wave_samples(on_s: float, off_s: float, cycles: int) -> bytearray:
    """Two-tone PSTN busy/reorder pattern (480 + 620 Hz)."""
    amp = 0.30
    samples = bytearray()
    period = on_s + off_s
    total_s = period * cycles
    n_samples = int(SAMPLE_RATE * total_s)
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        phase = t % period
        if phase < on_s:
            # 5 ms attack/release envelope per burst -> no clicks
            edge = min(1.0, phase / 0.005, (on_s - phase) / 0.005)
            edge = max(0.0, edge)
            value = amp * edge * (
                math.sin(2 * math.pi * 480 * t)
                + math.sin(2 * math.pi * 620 * t)
            ) / 2.0
        else:
            value = 0.0
        samples += struct.pack("<h", int(value * 32767))
    return samples


def _reject_wave_samples() -> bytearray:
    """Descending 660 -> 440 Hz sweep, ~0.8 s. Short 'nope' sound."""
    amp = 0.30
    duration_s = 0.8
    n_samples = int(SAMPLE_RATE * duration_s)
    samples = bytearray()
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        progress = t / duration_s
        freq = 660 - 220 * progress
        edge = min(1.0, t / 0.01, (duration_s - t) / 0.05)
        edge = max(0.0, edge)
        value = amp * edge * math.sin(2 * math.pi * freq * t)
        samples += struct.pack("<h", int(value * 32767))
    # 200 ms of silence so the QSoundEffect's loop=1 doesn't blip-end.
    samples += struct.pack("<h", 0) * int(SAMPLE_RATE * 0.2)
    return samples


def _write_wav(path: Path, samples: bytearray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(samples))


def ensure_failure_tones() -> tuple[Path, Path, Path]:
    """Generate the three failure tone WAVs on first run; return paths."""
    base = data_dir()
    busy_path = base / BUSY_TONE_FILENAME
    reorder_path = base / REORDER_TONE_FILENAME
    reject_path = base / REJECT_TONE_FILENAME
    if not busy_path.exists():
        _write_wav(busy_path, _busy_wave_samples(0.5, 0.5, 2))
        log.info("Generated busy tone at %s", busy_path)
    if not reorder_path.exists():
        _write_wav(reorder_path, _busy_wave_samples(0.25, 0.25, 4))
        log.info("Generated reorder tone at %s", reorder_path)
    if not reject_path.exists():
        _write_wav(reject_path, _reject_wave_samples())
        log.info("Generated reject tone at %s", reject_path)
    return busy_path, reorder_path, reject_path


def _tone_for_code(code: int | None) -> str | None:
    """Return 'busy' / 'reorder' / 'reject' / None for a SIP final code."""
    if code is None or code < 400:
        return None
    if code in (401, 407):           # auth -- silent (setup issue)
        return None
    if code in (486, 600):            # busy
        return "busy"
    if code in (487, 603, 480, 488, 604, 606):
        return "reject"
    # Everything else 4xx/5xx -> reorder (fast busy / congestion).
    return "reorder"


class FailureTone:
    """Plays the right PSTN-style tone once when a call ends in failure.

    Three pre-generated WAVs are loaded into separate QSoundEffect
    instances at construction so the tone fires with zero load latency
    when a call ends. Safe to construct in headless / no-QtMultimedia
    environments (becomes a no-op).

    Each tone is backed by a pool of N=3 QSoundEffect instances rotated
    round-robin per call. This avoids the QSoundEffect.stop()/play()
    race -- stop() is async, so replaying the same instance within one
    tick can drop or clip the onset. With 3 slots, hammering redial up
    to 3 times produces 3 clean overlapping tones; the 4th call wraps
    to slot 0, which by then has almost always finished its ~2 s tone.
    """

    _POOL_SIZE = 3

    def __init__(self) -> None:
        self._pools: dict[str, list[object]] = {}
        self._next_slot: dict[str, int] = {}
        self._available = False
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QSoundEffect

            busy, reorder, reject = ensure_failure_tones()
            for name, path in (
                ("busy", busy), ("reorder", reorder), ("reject", reject),
            ):
                url = QUrl.fromLocalFile(str(path))
                pool: list[object] = []
                for _ in range(self._POOL_SIZE):
                    fx = QSoundEffect()
                    fx.setSource(url)
                    fx.setLoopCount(1)
                    fx.setVolume(0.55)
                    pool.append(fx)
                self._pools[name] = pool
                self._next_slot[name] = 0
            self._available = True
        except Exception:
            log.warning(
                "Failure tones unavailable (QtMultimedia missing?); failed "
                "calls will be silent",
                exc_info=True,
            )

    @property
    def available(self) -> bool:
        return self._available

    def play_for_code(self, code: int | None) -> None:
        """Play the appropriate tone for `code`. No-op if no tone maps
        to this code (e.g. 200 success, 401/407 auth, or unknown)."""
        if not self._available:
            return
        name = _tone_for_code(code)
        if name is None:
            return
        pool = self._pools.get(name)
        if not pool:
            return
        try:
            slot = self._next_slot[name]
            self._next_slot[name] = (slot + 1) % self._POOL_SIZE
            fx = pool[slot]
            # Fire unconditionally on the rotated slot. We never reuse a
            # still-playing instance in the common case, so there's no
            # stop()/play() race. (If all 3 are somehow still active --
            # a 4th redial inside ~2 s -- the wrapped slot simply plays
            # over itself; QSoundEffect handles that gracefully.)
            fx.play()
        except Exception:
            log.exception("FailureTone.play_for_code(%s) failed", code)
