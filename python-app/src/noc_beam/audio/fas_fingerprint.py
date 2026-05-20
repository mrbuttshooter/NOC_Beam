"""Audio fingerprint via Chromaprint fpcalc.exe.

Shells out to the bundled fpcalc binary. Why not pyacoustid? It pulls in a
C extension that's awkward to ship via PyInstaller, and the binary
interface is simple.

Workflow:
    fingerprint_clip(samples, sample_rate) -> str | None
    FingerprintMemory().match(fp, account_id) -> bool

The memory keeps the last N fingerprints per (account_id, supplier) so a
second call to the same supplier returning the same canned audio is
detected as FINGERPRINT_REUSE.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from noc_beam._native.chromaprint import fpcalc_path

log = logging.getLogger(__name__)


def _write_wav_temp(samples: np.ndarray, sample_rate: int) -> Path:
    """Write a 16-bit mono WAV to a temp file. Caller deletes."""
    tmp = Path(tempfile.mkstemp(suffix=".wav", prefix="fas-fp-")[1])
    if samples.dtype != np.int16:
        samples = samples.astype(np.int16, copy=False)
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())
    return tmp


def fingerprint_clip(samples: np.ndarray, sample_rate: int = 16000) -> str | None:
    """Compute the Chromaprint fingerprint of an audio clip.

    Returns the compressed fingerprint string (base64-like) or None on
    failure. Failure is logged but never raised -- the caller can simply
    skip the fingerprint signal.
    """
    if samples.size < sample_rate:  # < 1 sec is not worth fingerprinting
        return None
    fp_bin = fpcalc_path()
    if not fp_bin.exists():
        return None
    wav_path = _write_wav_temp(samples, sample_rate)
    try:
        # CREATE_NO_WINDOW (Windows only; falls through to 0 elsewhere)
        # suppresses the black console flash that fpcalc.exe would
        # otherwise pop on every fingerprint pass — once per scoring
        # tick per call, very visible during live calls. The flag is
        # available since Python 3.7 on Windows; getattr keeps the
        # call portable for the (currently unused) Linux/macOS path.
        proc = subprocess.run(
            [str(fp_bin), "-raw", "-plain", "-length", "10", str(wav_path)],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            log.warning("fpcalc exit %s: %s", proc.returncode, proc.stderr.strip())
            return None
        out = proc.stdout.strip()
        # -raw emits integers space-separated. Compact via str() for hashing.
        return out
    except Exception:
        log.exception("fpcalc invocation failed")
        return None
    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass


def _parse_fp(s: str) -> list[int]:
    """fpcalc -raw -plain emits comma-separated int32s on a single line."""
    out: list[int] = []
    for token in s.replace(",", " ").split():
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            continue
    return out


def fingerprint_similarity(fp_a: str, fp_b: str) -> float:
    """Hamming similarity between two raw Chromaprint fingerprints.

    Each fingerprint is a sequence of int32 values; bits that match
    contribute to the similarity. Returns 0.0..1.0.
    """
    a = _parse_fp(fp_a)
    b = _parse_fp(fp_b)
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    matching_bits = 0
    total_bits = n * 32
    for i in range(n):
        xor = (a[i] ^ b[i]) & 0xFFFFFFFF
        # popcount
        matching_bits += 32 - bin(xor).count("1")
    return matching_bits / total_bits


@dataclass
class FingerprintEntry:
    fp: str
    when: float
    call_id: int
    account_id: str
    supplier: str


class FingerprintMemory:
    """Per-process rolling memory of recent fingerprints.

    Keeps the most recent ``maxlen`` entries. match() returns the best
    similarity score against any prior entry plus the matched entry.
    """

    def __init__(self, maxlen: int = 200, similarity_threshold: float = 0.90) -> None:
        self._entries: deque[FingerprintEntry] = deque(maxlen=maxlen)
        self.similarity_threshold = similarity_threshold

    def add(self, fp: str, *, call_id: int, account_id: str = "", supplier: str = "") -> None:
        if not fp:
            return
        # One call is scored repeatedly; keep its newest fingerprint only
        # so a long call does not crowd out recent calls from the memory.
        self._entries = deque(
            (entry for entry in self._entries if entry.call_id != call_id),
            maxlen=self._entries.maxlen,
        )
        self._entries.append(
            FingerprintEntry(
                fp=fp, when=time.time(), call_id=call_id,
                account_id=account_id, supplier=supplier,
            )
        )

    def match(
        self,
        fp: str,
        *,
        call_id: int,
        account_id: str = "",
        supplier: str = "",
    ) -> tuple[float, FingerprintEntry | None]:
        """Return (best_similarity, best_entry) ignoring the current call.

        Optionally scopes to entries with the same account_id / supplier --
        passing empty strings means "compare against all entries".
        """
        if not fp:
            return 0.0, None
        best_sim = 0.0
        best_entry: FingerprintEntry | None = None
        for e in self._entries:
            if e.call_id == call_id:
                continue
            if account_id and e.account_id != account_id:
                continue
            if supplier and e.supplier != supplier:
                continue
            sim = fingerprint_similarity(fp, e.fp)
            if sim > best_sim:
                best_sim = sim
                best_entry = e
        return best_sim, best_entry


_memory: FingerprintMemory | None = None


def fingerprint_memory() -> FingerprintMemory:
    global _memory
    if _memory is None:
        _memory = FingerprintMemory()
    return _memory
