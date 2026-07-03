"""Per-call ring buffers for FAS audio.

PJSIP audio threads write incoming 20 ms PCM frames into per-call ring
buffers. A Qt worker thread reads chunks out for inference. Each call gets
its own buffer (typically 10 seconds = 320 KB int16 mono @ 16 kHz);
buffers are torn down when the call disconnects.

The router is the only place that touches both writer (PJSIP) and reader
(worker) threads, so it owns the lock. Buffers are pre-allocated numpy
arrays — no allocation on the audio path.

Frame layout: int16 little-endian mono @ FAS_SAMPLE_RATE (16 kHz).
"""
from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

from noc_beam.audio.fas_tap import FAS_SAMPLE_RATE

log = logging.getLogger(__name__)

# 10 seconds rolling window per call. Enough for any single inference pass
# plus context for fingerprint matching. Memory cost: 10 * 16000 * 2 = 320 KB.
RING_SECONDS = 10
RING_SAMPLES = RING_SECONDS * FAS_SAMPLE_RATE


class _CallRingBuffer:
    """Single-producer / single-consumer ring of int16 samples.

    Writes happen on PJSIP audio threads (one per call). Reads happen on
    the FAS worker thread. The lock is held only briefly for each op.
    """

    __slots__ = ("_buf", "_write_pos", "_total_samples", "_lock")

    def __init__(self) -> None:
        self._buf = np.zeros(RING_SAMPLES, dtype=np.int16)
        self._write_pos = 0
        self._total_samples = 0  # monotonic: never decreases
        self._lock = threading.Lock()

    def push_bytes(self, data: bytes) -> None:
        """Write a PCM frame (int16 LE bytes) into the ring."""
        if not data:
            return
        samples = np.frombuffer(data, dtype=np.int16)
        with self._lock:
            n = samples.size
            if n >= RING_SAMPLES:
                # Frame larger than ring -- keep the last RING_SAMPLES.
                self._buf[:] = samples[-RING_SAMPLES:]
                self._write_pos = 0
                self._total_samples += n
                return
            end = self._write_pos + n
            if end <= RING_SAMPLES:
                self._buf[self._write_pos:end] = samples
            else:
                first = RING_SAMPLES - self._write_pos
                self._buf[self._write_pos:] = samples[:first]
                self._buf[: n - first] = samples[first:]
            self._write_pos = end % RING_SAMPLES
            self._total_samples += n

    def snapshot(self, seconds: float | None = None) -> np.ndarray:
        """Return a contiguous copy of the most recent `seconds` of audio.

        If seconds is None, returns the full ring. Returned shape: (n,) int16.
        If fewer samples have been written than requested, returns whatever
        is available (length-truncated, not zero-padded).
        """
        n = int(seconds * FAS_SAMPLE_RATE) if seconds is not None else RING_SAMPLES
        n = min(n, RING_SAMPLES)
        with self._lock:
            available = min(self._total_samples, RING_SAMPLES)
            if available == 0:
                return np.zeros(0, dtype=np.int16)
            n = min(n, available)
            if self._total_samples < RING_SAMPLES:
                # Ring not yet wrapped; data is [0 : write_pos).
                return self._buf[self._write_pos - n: self._write_pos].copy()
            # Exactly-full (_total_samples == RING_SAMPLES) belongs on the
            # wrapped path, NOT the not-yet-wrapped one: write_pos has just
            # wrapped to 0, so self._buf[write_pos - n : write_pos] would be
            # self._buf[-n:0] -- an EMPTY slice, returning nothing from a
            # completely full buffer. The modular arithmetic below handles
            # write_pos == 0 correctly (start = (0 - n) % RING_SAMPLES).
            # Wrapped. Data is conceptually (write_pos .. write_pos + RING)
            # mod RING. We want the most recent n samples ending at write_pos.
            start = (self._write_pos - n) % RING_SAMPLES
            if start + n <= RING_SAMPLES:
                return self._buf[start:start + n].copy()
            tail = RING_SAMPLES - start
            out = np.empty(n, dtype=np.int16)
            out[:tail] = self._buf[start:]
            out[tail:] = self._buf[: n - tail]
            return out

    @property
    def total_samples_written(self) -> int:
        with self._lock:
            return self._total_samples


class FasAudioRouter:
    """Process-wide singleton owning per-call ring buffers."""

    def __init__(self) -> None:
        self._buffers: dict[int, _CallRingBuffer] = {}
        self._meta: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle (Qt main thread)
    # ------------------------------------------------------------------
    def attach(self, call_id: int, **meta: Any) -> None:
        """Register a call for tapping. Idempotent."""
        with self._lock:
            if call_id in self._buffers:
                return
            self._buffers[call_id] = _CallRingBuffer()
            self._meta[call_id] = dict(meta)
        log.debug("FasAudioRouter attached call %s meta=%s", call_id, meta)

    def detach(self, call_id: int) -> None:
        """Release a call's buffer + meta. Idempotent."""
        with self._lock:
            self._buffers.pop(call_id, None)
            self._meta.pop(call_id, None)
        log.debug("FasAudioRouter detached call %s", call_id)

    def teardown(self) -> None:
        """Drop everything. Call during endpoint shutdown."""
        with self._lock:
            self._buffers.clear()
            self._meta.clear()

    # ------------------------------------------------------------------
    # PJSIP audio thread
    # ------------------------------------------------------------------
    def push(self, call_id: int, data: bytes) -> None:
        # Snapshot the ring out of the lock so push_bytes can lock its own.
        with self._lock:
            ring = self._buffers.get(call_id)
        if ring is None:
            return
        ring.push_bytes(data)

    # ------------------------------------------------------------------
    # Worker thread reads
    # ------------------------------------------------------------------
    def snapshot(self, call_id: int, seconds: float | None = None) -> np.ndarray:
        with self._lock:
            ring = self._buffers.get(call_id)
        if ring is None:
            return np.zeros(0, dtype=np.int16)
        return ring.snapshot(seconds)

    def total_samples(self, call_id: int) -> int:
        with self._lock:
            ring = self._buffers.get(call_id)
        if ring is None:
            return 0
        return ring.total_samples_written

    def meta(self, call_id: int) -> dict[str, Any]:
        with self._lock:
            return dict(self._meta.get(call_id, {}))

    def active_calls(self) -> list[int]:
        with self._lock:
            return list(self._buffers.keys())


_router: FasAudioRouter | None = None


def fas_router() -> FasAudioRouter:
    global _router
    if _router is None:
        _router = FasAudioRouter()
    return _router
