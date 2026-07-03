"""Ring buffer correctness for fas_router._CallRingBuffer.

Critical because PJSIP audio threads write into it from one thread while
the worker thread reads from another. Wrap-around bugs would silently
corrupt the inference input.
"""
from __future__ import annotations

import numpy as np

from noc_beam.audio.fas_router import RING_SAMPLES, _CallRingBuffer
from noc_beam.audio.fas_tap import FAS_SAMPLE_RATE


def _frame(start: int, count: int) -> bytes:
    """A monotonically-increasing int16 frame, easy to verify.

    Built via int32 + astype(int16) so values past 32767 wrap silently
    instead of raising under numpy 2.x's stricter overflow checks.
    """
    return np.arange(start, start + count, dtype=np.int32).astype(np.int16).tobytes()


def test_empty_snapshot_returns_zero_length():
    ring = _CallRingBuffer()
    out = ring.snapshot()
    assert out.shape == (0,)
    assert out.dtype == np.int16


def test_single_frame_round_trip():
    ring = _CallRingBuffer()
    ring.push_bytes(_frame(0, 320))  # one 20 ms frame
    snap = ring.snapshot()
    assert snap.shape == (320,)
    assert snap[0] == 0
    assert snap[-1] == 319


def test_snapshot_seconds_truncates_to_available():
    ring = _CallRingBuffer()
    ring.push_bytes(_frame(0, 320))
    # Asking for 3s but only 20ms written -> returns what's available.
    snap = ring.snapshot(seconds=3.0)
    assert snap.shape == (320,)


def test_snapshot_seconds_returns_most_recent():
    ring = _CallRingBuffer()
    # Push 2 seconds of frames (100 frames * 320 samples = 32000 samples = 2s)
    for i in range(100):
        ring.push_bytes(_frame(i * 320, 320))
    snap = ring.snapshot(seconds=1.0)
    assert snap.shape == (FAS_SAMPLE_RATE,)
    # Last sample should be 99*320 + 319 = 31999
    assert snap[-1] == 31999
    # The 16000 most recent samples end at index 31999, so they start at 16000.
    assert snap[0] == 16000


def test_wrap_around_preserves_chronological_order():
    ring = _CallRingBuffer()
    # Fill exactly RING_SAMPLES then push one more frame to force wrap.
    total = RING_SAMPLES + 1000
    chunk = 320
    pushed = 0
    counter = 0
    while pushed < total:
        n = min(chunk, total - pushed)
        ring.push_bytes(_frame(counter, n))
        counter += n
        pushed += n
    snap = ring.snapshot()
    assert snap.shape == (RING_SAMPLES,)
    # The most recent sample written was counter-1.
    # In int16, large monotonic values wrap so compare via int16 round-trip.
    expected_last = np.array([counter - 1], dtype=np.int32).astype(np.int16)[0]
    assert snap[-1] == expected_last
    # And the snapshot must be strictly contiguous (no gap from wrap).
    # Diff in int32 space yields +1 everywhere EXCEPT where the int16
    # wrap point falls (32767 -> -32768 is a -65535 jump).
    diffs = np.diff(snap.astype(np.int32))
    contiguous = np.where(diffs == 1, True, diffs == -65535)
    assert contiguous.all()


def test_exactly_full_buffer_returns_full_snapshot():
    """Regression: when _total_samples == RING_SAMPLES exactly, write_pos has
    just wrapped to 0. The old not-yet-wrapped slice [-n:0] returned EMPTY
    from a completely full buffer. Snapshot must return all RING_SAMPLES."""
    ring = _CallRingBuffer()
    chunk = 320
    pushed = 0
    counter = 0
    while pushed < RING_SAMPLES:
        n = min(chunk, RING_SAMPLES - pushed)
        ring.push_bytes(_frame(counter, n))
        counter += n
        pushed += n
    assert ring.total_samples_written == RING_SAMPLES
    snap = ring.snapshot()
    assert snap.shape == (RING_SAMPLES,), "full buffer must not return empty"
    expected_last = np.array([RING_SAMPLES - 1], dtype=np.int32).astype(np.int16)[0]
    assert snap[-1] == expected_last
    # A partial (seconds-scoped) snapshot from the exactly-full buffer must
    # also return the most-recent tail, not empty.
    partial = ring.snapshot(seconds=1.0)
    assert partial.shape == (FAS_SAMPLE_RATE,)
    part_last = np.array([RING_SAMPLES - 1], dtype=np.int32).astype(np.int16)[0]
    assert partial[-1] == part_last


def test_oversize_frame_keeps_only_tail():
    ring = _CallRingBuffer()
    # Push a frame larger than the ring; only the last RING_SAMPLES should land.
    big = _frame(0, RING_SAMPLES + 500)
    ring.push_bytes(big)
    snap = ring.snapshot()
    assert snap.shape == (RING_SAMPLES,)
    expected_first = np.array([500], dtype=np.int32).astype(np.int16)[0]
    expected_last = np.array([RING_SAMPLES + 499], dtype=np.int32).astype(np.int16)[0]
    assert snap[0] == expected_first
    assert snap[-1] == expected_last


def test_total_samples_written_is_monotonic():
    ring = _CallRingBuffer()
    assert ring.total_samples_written == 0
    ring.push_bytes(_frame(0, 320))
    assert ring.total_samples_written == 320
    ring.push_bytes(_frame(320, 320))
    assert ring.total_samples_written == 640


def test_router_attach_detach_idempotent():
    from noc_beam.audio.fas_router import FasAudioRouter

    r = FasAudioRouter()
    r.attach(42, codec="PCMU")
    r.attach(42, codec="PCMA")  # second attach is no-op (does not reset buffer)
    r.push(42, _frame(0, 320))
    assert r.total_samples(42) == 320
    r.detach(42)
    r.detach(42)  # double-detach is fine
    assert r.total_samples(42) == 0
    # Push to detached call is a no-op, not an error
    r.push(42, _frame(0, 320))
    assert r.total_samples(42) == 0
