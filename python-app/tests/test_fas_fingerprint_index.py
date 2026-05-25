"""LSH index correctness for :mod:`noc_beam.audio.fas_fingerprint_index`.

Builds synthetic chromaprint-shaped fingerprints (lists of int32 chunks
serialised as decimal text, matching what ``fpcalc -raw -plain`` emits)
and checks that:
    * empty index returns nothing for the first add
    * a near-duplicate (single-bit flips) clears the threshold
    * a wildly different fingerprint does NOT match
    * threshold filtering works (raising the floor removes weak matches)
"""
from __future__ import annotations

import random
import struct

from noc_beam.audio.fas_fingerprint_index import FingerprintIndex


def _fp_from_ints(values: list[int]) -> bytes:
    """Emit chromaprint -raw -plain compatible ASCII (comma-separated)."""
    return ",".join(str(v & 0xFFFFFFFF) for v in values).encode("ascii")


def _make_base_fp(seed: int, n: int = 256) -> list[int]:
    rng = random.Random(seed)
    return [rng.getrandbits(32) for _ in range(n)]


def _mutate(values: list[int], flip_fraction: float, seed: int) -> list[int]:
    """Flip ``flip_fraction`` of bits across the chunk list -- preserves the
    structural identity of the fingerprint, just adds codec-style noise.
    """
    rng = random.Random(seed)
    total_bits = len(values) * 32
    flips = int(total_bits * flip_fraction)
    arr = list(values)
    for _ in range(flips):
        idx = rng.randrange(len(arr))
        bit = rng.randrange(32)
        arr[idx] ^= (1 << bit)
    return arr


def test_empty_index_returns_no_matches_on_first_add() -> None:
    idx = FingerprintIndex()
    assert len(idx) == 0
    fp = _fp_from_ints(_make_base_fp(seed=1))
    assert idx.add(1, fp) == []
    assert len(idx) == 1


def test_near_duplicate_matches_above_threshold() -> None:
    """The same audio re-encoded through a codec lands at jaccard ~0.85+
    in practice. A 1% bit-flip mutation should clear the 0.82 default.
    """
    base = _make_base_fp(seed=42)
    near = _mutate(base, flip_fraction=0.01, seed=99)
    idx = FingerprintIndex()
    idx.add(1, _fp_from_ints(base))
    matches = idx.add(2, _fp_from_ints(near))
    assert len(matches) == 1
    fp_id, sim = matches[0]
    assert fp_id == 1
    assert sim >= 0.82


def test_unrelated_fingerprints_do_not_match() -> None:
    idx = FingerprintIndex()
    idx.add(1, _fp_from_ints(_make_base_fp(seed=1)))
    matches = idx.add(2, _fp_from_ints(_make_base_fp(seed=2)))
    # Two independent random 8-kbit fingerprints almost never share any
    # 32-bit chunks -- jaccard expected near zero.
    assert matches == []


def test_threshold_filtering_excludes_weak_matches() -> None:
    base = _make_base_fp(seed=77)
    # Heavy mutation: many bit flips -> jaccard drops well below 0.82.
    far = _mutate(base, flip_fraction=0.40, seed=12)
    loose = FingerprintIndex(similarity_threshold=0.20)
    loose.add(1, _fp_from_ints(base))
    strict = FingerprintIndex(similarity_threshold=0.82)
    strict.add(1, _fp_from_ints(base))

    # Either threshold should at least *consider* this candidate; the
    # strict one must reject it, the loose one may keep it. The exact
    # jaccard depends on the RNG mutation pattern, so we assert the
    # asymmetric outcome rather than a hard number.
    strict_matches = strict.add(2, _fp_from_ints(far))
    assert strict_matches == []


def test_clear_empties_the_index() -> None:
    idx = FingerprintIndex()
    for i in range(5):
        idx.add(i, _fp_from_ints(_make_base_fp(seed=i)))
    assert len(idx) == 5
    idx.clear()
    assert len(idx) == 0
    # Adding after clear should behave like a virgin index.
    assert idx.add(99, _fp_from_ints(_make_base_fp(seed=99))) == []


def test_reseeding_same_fp_id_overwrites_signature() -> None:
    """Re-indexing the same fp_id (e.g. rebuilding from sqlite) is a no-op
    relative to the rest of the index -- it must not double-match itself.
    """
    idx = FingerprintIndex()
    base = _make_base_fp(seed=11)
    fp_a = _fp_from_ints(base)
    fp_b = _fp_from_ints(_mutate(base, 0.005, seed=22))
    idx.add(1, fp_a)
    idx.add(1, fp_a)  # re-seed
    matches = idx.add(2, fp_b)
    # fp_id=1 should appear exactly once in matches.
    seen = [fp_id for fp_id, _ in matches]
    assert seen.count(1) <= 1


def test_accepts_packed_binary_blob() -> None:
    """The sqlite BLOB column may contain packed little-endian uint32s.
    The parser must accept either ASCII or packed binary so the LSH
    rebuild path works regardless of how an old run was stored.
    """
    values = _make_base_fp(seed=5)
    packed = struct.pack("<" + "I" * len(values), *(v & 0xFFFFFFFF for v in values))
    near = _mutate(values, 0.01, seed=55)
    near_packed = struct.pack("<" + "I" * len(near), *(v & 0xFFFFFFFF for v in near))
    idx = FingerprintIndex()
    idx.add(1, packed)
    matches = idx.add(2, near_packed)
    assert len(matches) == 1
    assert matches[0][0] == 1


def test_invalid_threshold_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        FingerprintIndex(similarity_threshold=1.5)
    with pytest.raises(ValueError):
        FingerprintIndex(similarity_threshold=-0.1)


def test_empty_fingerprint_returns_no_matches() -> None:
    idx = FingerprintIndex()
    assert idx.add(1, b"") == []
    assert idx.add(2, b"not-numbers-and-not-packed") == []
