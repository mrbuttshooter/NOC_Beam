"""LSH index over Chromaprint fingerprints for fast cross-supplier matching.

The single-supplier fingerprint memory in :mod:`noc_beam.audio.fas_fingerprint`
does an O(N) hamming compare against the rolling window. That's fine for the
live call card. For a sweep that runs hundreds of calls across dozens of
suppliers, we want to know -- in near-constant time -- whether a freshly
captured fingerprint *also* appears on a different supplier (the hallmark
of a route that's farming canned audio to multiple buyers).

Implementation notes:
    * Chromaprint ``-raw`` output is a list of int32 chunks, each chunk
      derived from a ~125 ms window. Two recordings of the same source
      audio produce mostly-identical chunk sequences with small bit
      differences from codec/jitter.
    * Similarity is bit-hamming over the int32 sequences (the same
      metric :func:`fas_fingerprint.fingerprint_similarity` uses for
      the live call card). LSH is built by slicing each fingerprint
      into ``_BANDS`` horizontal bit-bands and hashing each band; two
      fingerprints that share a single band are surfaced as candidates,
      then rescored exactly. This catches the small per-chunk bit flips
      that codec hops introduce while still avoiding the O(N*M) full
      compare against every prior fingerprint.
    * The threshold defaults to 0.82 -- the new value Agent A landed.
      In practice carrier-transcoded audio of the same clip lands in
      the 0.82 - 0.92 hamming band; clean recordings hit 0.95+.

Pure stdlib. No persistence -- the index is rebuilt at the start of
each sweep from the sqlite ``fingerprints`` table (which always
survives, the index does not).
"""
from __future__ import annotations

import hashlib
import struct


# LSH bucketing strategy for Chromaprint fingerprints. We extract
# per-chunk "low-resolution" keys by taking the high N bits of each
# int32 chunk (the low bits flip first under codec noise), then index
# every chunk position into a (position, hi_bits) bucket. Two
# fingerprints become candidates if they share at least
# ``_MIN_SHARED_CHUNKS`` such positions in the same place -- a very
# loose filter, but the exact hamming rescore that follows tightens it
# back up. This survives the ~1-3% bit-flip rate that G.711/codec hops
# introduce because individual chunk hi-bits are stable when the
# underlying audio is.
_HI_BITS = 24                # keep the top 24 bits of each int32 chunk
_HI_MASK = ((1 << _HI_BITS) - 1) << (32 - _HI_BITS)
_MIN_SHARED_CHUNKS = 4       # candidacy threshold


def _parse_raw_fingerprint(fp: bytes) -> list[int]:
    """Parse Chromaprint -raw -plain ASCII output (or raw int32 blob).

    Accepts two encodings:
        * ASCII bytes from ``fpcalc -raw -plain`` -- comma/whitespace separated
          integers. This is what :mod:`fas_fingerprint` produces today.
        * Packed little-endian uint32 blob (multiple of 4 bytes). The
          sqlite ``fingerprints.fingerprint`` column is stored as BLOB
          and could be either form; we accept both so callers don't have
          to convert.

    Returns the chunk values as a list of unsigned 32-bit ints.
    """
    if not fp:
        return []
    # Try ASCII first; the byte set of decimal digits / commas / whitespace
    # is disjoint from most packed-binary content.
    try:
        text = fp.decode("ascii")
    except UnicodeDecodeError:
        text = ""
    if text and all(c.isdigit() or c in "-+,. \t\r\n" for c in text):
        out: list[int] = []
        for tok in text.replace(",", " ").split():
            tok = tok.strip()
            if not tok:
                continue
            try:
                v = int(tok)
            except ValueError:
                continue
            out.append(v & 0xFFFFFFFF)
        if out:
            return out
    # Fall back to packed little-endian uint32.
    if len(fp) >= 4:
        n = len(fp) // 4
        return list(struct.unpack("<" + "I" * n, bytes(fp[: n * 4])))
    return []


def _hamming_similarity(chunks_a: list[int], chunks_b: list[int]) -> float:
    """Bit-hamming similarity in [0, 1] between two int32 chunk lists.

    Matches the metric used by :func:`fas_fingerprint.fingerprint_similarity`
    -- so the LSH rescore stays consistent with the live call card.
    """
    if not chunks_a or not chunks_b:
        return 0.0
    n = min(len(chunks_a), len(chunks_b))
    if n == 0:
        return 0.0
    matching = 0
    total = n * 32
    for i in range(n):
        xor = (chunks_a[i] ^ chunks_b[i]) & 0xFFFFFFFF
        matching += 32 - bin(xor).count("1")
    return matching / total


def _bucket_keys(chunks: list[int]) -> list[tuple[int, int]]:
    """Per-position bucket keys for a chunk list.

    Returns ``(chunk_idx, hi_bits)`` for every chunk; the index keeps a
    map of these to fp_id sets, so two fingerprints are candidates iff
    they share at least one position with the same hi-bits value. The
    hi-bits-only projection is what gives the filter its codec
    robustness.
    """
    return [(i, c & _HI_MASK) for i, c in enumerate(chunks)]


class FingerprintIndex:
    """LSH-backed fingerprint index.

    Each :meth:`add` returns the list of already-indexed fingerprints
    whose estimated jaccard similarity is at or above the configured
    threshold. The intended workflow for the sweep runner is:

        idx = FingerprintIndex()
        for (fp_id, fp_bytes) in db.get_fingerprints_for_run(run_id):
            idx.add(fp_id, fp_bytes)  # seed from history

        # Per new call:
        matches = idx.add(new_fp_id, new_fp_bytes)
        for prior_fp_id, sim in matches:
            db.record_match(prior_fp_id, new_fp_id, sim)

    The index keeps the fingerprint signature in memory (1 KiB per
    entry) but does NOT keep the raw chunks; rescoring a match means
    re-reading the BLOB from sqlite.
    """

    def __init__(self, similarity_threshold: float = 0.82) -> None:
        if not (0.0 <= similarity_threshold <= 1.0):
            raise ValueError(
                "similarity_threshold must be in [0.0, 1.0]; got %r"
                % (similarity_threshold,)
            )
        self.similarity_threshold = similarity_threshold
        # fp_id -> chunk list (kept in memory so the rescore doesn't
        # have to round-trip sqlite for every candidate)
        self._chunks: dict[int, list[int]] = {}
        # (chunk_idx, hi_bits) -> set of fp_ids that share this position
        self._buckets: dict[tuple[int, int], set[int]] = {}

    def __len__(self) -> int:
        return len(self._chunks)

    def add(self, fp_id: int, fingerprint: bytes) -> list[tuple[int, float]]:
        """Index ``fingerprint`` under ``fp_id`` and report near-duplicates.

        Returns a list of ``(existing_fp_id, similarity)`` tuples sorted
        by similarity descending. Empty list if no candidate clears the
        threshold OR if the fingerprint failed to parse.

        Calling ``add(same_fp_id, ...)`` overwrites the prior chunk list
        (so re-seeding from sqlite is idempotent).
        """
        chunks = _parse_raw_fingerprint(fingerprint)
        if not chunks:
            return []

        # If this fp_id was previously indexed, evict its old bucket
        # postings so we don't leave phantom slots after re-seed.
        if int(fp_id) in self._chunks:
            old_chunks = self._chunks[int(fp_id)]
            for key in _bucket_keys(old_chunks):
                bucket = self._buckets.get(key)
                if bucket is not None:
                    bucket.discard(int(fp_id))
                    if not bucket:
                        self._buckets.pop(key, None)

        # Candidacy pass: tally how many positions each existing fp
        # shares with this one. Anything with >= _MIN_SHARED_CHUNKS is
        # worth a full hamming rescore.
        share_count: dict[int, int] = {}
        keys = _bucket_keys(chunks)
        for key in keys:
            bucket = self._buckets.get(key)
            if not bucket:
                continue
            for other_id in bucket:
                if other_id == int(fp_id):
                    continue
                share_count[other_id] = share_count.get(other_id, 0) + 1

        # Full hamming rescore on the candidates.
        matches: list[tuple[int, float]] = []
        for cand_id, shared in share_count.items():
            if shared < _MIN_SHARED_CHUNKS:
                continue
            cand_chunks = self._chunks.get(cand_id)
            if cand_chunks is None:
                continue
            sim = _hamming_similarity(chunks, cand_chunks)
            if sim >= self.similarity_threshold:
                matches.append((cand_id, sim))
        matches.sort(key=lambda t: t[1], reverse=True)

        # Insert AFTER scoring so we don't match against ourselves.
        self._chunks[int(fp_id)] = chunks
        for key in keys:
            self._buckets.setdefault(key, set()).add(int(fp_id))

        return matches

    def clear(self) -> None:
        self._chunks.clear()
        self._buckets.clear()
