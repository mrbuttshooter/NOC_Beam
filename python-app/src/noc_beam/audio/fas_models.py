"""ONNX model wrappers for FAS detection.

Lazy-loaded singletons; each wrapper exposes a single ``score(samples)``
method. If the underlying .onnx file is missing or onnxruntime fails to
load, ``score`` returns None and the rules engine treats the signal as
"not observed" rather than crashing.

Models:
    SileroVad        - per-frame speech probability (16 kHz mono)
    AasistDetector   - synthetic / recorded-audio probability (16 kHz mono)
    PannsClassifier  - 527-class AudioSet probabilities (32 kHz mono).
                       Exposes high-level convenience: music_score,
                       speech_score, noise_score derived from class indices.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from noc_beam.audio.models import model_path

log = logging.getLogger(__name__)


def _try_import_ort() -> Any | None:
    try:
        import onnxruntime as ort  # type: ignore[import-untyped]

        return ort
    except Exception:
        return None


def _resample_polyphase(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear interpolation resample. Adequate for feature input; we don't
    need audiophile quality because the model has to be robust to codec
    artefacts anyway."""
    if src_rate == dst_rate or x.size == 0:
        return x.astype(np.float32, copy=False)
    src_idx = np.linspace(0, x.size - 1, num=int(x.size * dst_rate / src_rate))
    lo = np.floor(src_idx).astype(np.int64)
    hi = np.minimum(lo + 1, x.size - 1)
    frac = (src_idx - lo).astype(np.float32)
    a = x[lo].astype(np.float32)
    b = x[hi].astype(np.float32)
    return a * (1.0 - frac) + b * frac


class _BaseModel:
    _onnx_filename = ""  # subclass

    def __init__(self) -> None:
        self._sess: Any = None
        self._tried = False

    @property
    def available(self) -> bool:
        self._maybe_load()
        return self._sess is not None

    def _maybe_load(self) -> None:
        if self._tried:
            return
        self._tried = True
        ort = _try_import_ort()
        if ort is None:
            log.warning("%s: onnxruntime not available", type(self).__name__)
            return
        path = model_path(self._onnx_filename)
        if not path.exists():
            log.warning("%s: %s not found at %s", type(self).__name__,
                        self._onnx_filename, path)
            return
        # ONNX external-data sidecar pre-flight. PANNs (Cnn14_16k) stores
        # weights in Cnn14_16k.onnx.data next to the .onnx graph. If the
        # PyInstaller bundle is from before the build/noc_beam.spec fix
        # that globs *.onnx.data, ort.InferenceSession() raises with a
        # noisy "External data path does not exist" stack. Skip cleanly
        # instead: SileroVad still loads, the rules engine still runs,
        # FAS verdicts just lose the music/noise signal. Detect by
        # checking for an adjacent .data sidecar when the .onnx is
        # suspiciously small (<200 KB -- the graph-only files are tiny
        # whereas embedded-weights models are tens of MB).
        try:
            data_sidecar = path.with_name(path.name + ".data")
            graph_kb = path.stat().st_size // 1024
            if graph_kb < 200 and not data_sidecar.exists():
                log.warning(
                    "%s: external-data sidecar missing (%s); skipping. "
                    "Your build is missing the .onnx.data file -- rebuild "
                    "with build/noc_beam.spec from commit ba369d8 or later.",
                    type(self).__name__, data_sidecar,
                )
                return
        except Exception:
            # Pre-flight is best-effort; fall through to the real load
            # and let any exception path log.exception as before.
            pass
        try:
            self._sess = ort.InferenceSession(
                str(path), providers=["CPUExecutionProvider"]
            )
            log.info("%s loaded from %s", type(self).__name__, path)
        except Exception:
            log.exception("%s: failed to load %s", type(self).__name__, path)
            self._sess = None


class SileroVad(_BaseModel):
    """Per-clip aggregated speech probability."""

    _onnx_filename = "silero_vad.onnx"

    def __init__(self) -> None:
        super().__init__()
        # Silero is stateful; we run it stateless per call by re-seeding state.
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def score(self, samples: np.ndarray, sample_rate: int = 16000) -> float | None:
        """Return mean speech probability across the clip in 0..1, or None."""
        self._maybe_load()
        if self._sess is None or samples.size == 0:
            return None
        if sample_rate != 16000:
            samples = _resample_polyphase(samples, sample_rate, 16000)
            sample_rate = 16000
        if samples.dtype == np.int16:
            x = samples.astype(np.float32) / 32768.0
        else:
            x = samples.astype(np.float32, copy=False)
        # Silero expects 512-sample frames at 16 kHz (32 ms).
        frame_size = 512
        n_frames = x.size // frame_size
        if n_frames == 0:
            return None
        state = np.zeros((2, 1, 128), dtype=np.float32)
        sr = np.array(16000, dtype=np.int64)
        probs = []
        try:
            for i in range(n_frames):
                frame = x[i * frame_size:(i + 1) * frame_size].reshape(1, -1)
                out = self._sess.run(None, {"input": frame, "state": state, "sr": sr})
                probs.append(float(out[0][0][0]))
                state = out[1]
        except Exception:
            log.exception("SileroVad inference failed")
            return None
        if not probs:
            return None
        return float(np.mean(probs))


class AasistDetector(_BaseModel):
    """AASIST anti-spoofing. Returns spoof-probability 0..1 or None."""

    _onnx_filename = "aasist.onnx"

    def score(self, samples: np.ndarray, sample_rate: int = 16000) -> float | None:
        self._maybe_load()
        if self._sess is None or samples.size == 0:
            return None
        if sample_rate != 16000:
            samples = _resample_polyphase(samples, sample_rate, 16000)
        if samples.dtype == np.int16:
            x = samples.astype(np.float32) / 32768.0
        else:
            x = samples.astype(np.float32, copy=False)
        # AASIST typically expects 4 seconds at 16 kHz = 64000 samples.
        target = 64000
        if x.size < target:
            x = np.pad(x, (0, target - x.size))
        else:
            x = x[:target]
        x = x.reshape(1, -1).astype(np.float32)
        try:
            inputs = self._sess.get_inputs()
            input_name = inputs[0].name if inputs else "input"
            out = self._sess.run(None, {input_name: x})
            logits = out[0].squeeze()
            # AASIST convention: index 1 = spoof, index 0 = bonafide.
            if logits.ndim == 0:
                return float(1.0 / (1.0 + np.exp(-logits)))
            ex = np.exp(logits - np.max(logits))
            sm = ex / ex.sum()
            if sm.size >= 2:
                return float(sm[1])
            return float(sm[0])
        except Exception:
            log.exception("AasistDetector inference failed")
            return None


class PannsClassifier(_BaseModel):
    """PANNs CNN14: 527-class AudioSet probabilities. Returns dict subset or None.

    Uses the 16 kHz variant (Cnn14_16k.onnx) -- input goes in at our pipeline's
    native rate, no resampling step needed. Weights live in a companion
    .onnx.data file that ONNX Runtime loads transparently when present in the
    same directory.
    """

    _onnx_filename = "Cnn14_16k.onnx"

    # AudioSet class indices we care about (subset)
    SPEECH_IDX = (0, 1, 2, 3, 4)            # Speech, Male, Female, etc.
    MUSIC_IDX = (137, 138, 139, 140, 141)   # Music, Pop, Classical, ...
    RINGING_IDX = (390, 391, 392, 393)      # Telephone bell, Ringtone, ...
    SILENCE_IDX = (494,)                     # Silence
    NOISE_IDX = (513, 514, 515, 516)        # White/Pink/Brownian noise, Static

    def score(self, samples: np.ndarray, sample_rate: int = 16000) -> dict[str, float] | None:
        self._maybe_load()
        if self._sess is None or samples.size == 0:
            return None
        # 16 kHz variant -- no resample step needed for our pipeline.
        if sample_rate != 16000:
            samples = _resample_polyphase(samples, sample_rate, 16000)
        if samples.dtype == np.int16:
            x = samples.astype(np.float32) / 32768.0
        else:
            x = samples.astype(np.float32, copy=False)
        # PANNs CNN14 trained on ~10s clips; truncating to 1s gave noisy scores.
        # Take most-recent samples (caller passes a rolling window).
        # CNN14 accepts variable-length input via its built-in pooling.
        target = min(x.size, 16000 * 10)
        if x.size < 16000:
            # Need at least ~1s to produce meaningful features; pad if shorter.
            x = np.pad(x, (0, 16000 - x.size))
        else:
            x = x[-target:]
        x = x.reshape(1, -1).astype(np.float32)
        try:
            inputs = self._sess.get_inputs()
            input_name = inputs[0].name if inputs else "input"
            out = self._sess.run(None, {input_name: x})
            probs = out[0].squeeze()
            if probs.ndim != 1:
                return None
            return {
                "speech": float(probs[list(self.SPEECH_IDX)].max()),
                "music": float(probs[list(self.MUSIC_IDX)].max()),
                "ringing": float(probs[list(self.RINGING_IDX)].max()),
                "silence": float(probs[self.SILENCE_IDX[0]]),
                "noise": float(probs[list(self.NOISE_IDX)].max()),
            }
        except Exception:
            log.exception("PannsClassifier inference failed")
            return None


_silero: SileroVad | None = None
_aasist: AasistDetector | None = None
_panns: PannsClassifier | None = None


def silero_vad() -> SileroVad:
    global _silero
    if _silero is None:
        _silero = SileroVad()
    return _silero


def aasist_detector() -> AasistDetector:
    global _aasist
    if _aasist is None:
        _aasist = AasistDetector()
    return _aasist


def panns_classifier() -> PannsClassifier:
    global _panns
    if _panns is None:
        _panns = PannsClassifier()
    return _panns


def shutdown_models() -> None:
    """Release ONNX InferenceSession references held by module-level singletons.

    Intended to be called from ``fas_engine.stop_fas_engine()`` so that a
    subsequent worker start (e.g. in test runs reusing the same Python
    interpreter) re-initialises cleanly instead of retaining stale sessions.
    Nulls each holder's ``_sess`` attribute first to encourage onnxruntime
    cleanup before dropping the singleton reference.
    """
    global _silero, _aasist, _panns
    for holder in (_silero, _aasist, _panns):
        if holder is not None and getattr(holder, "_sess", None) is not None:
            holder._sess = None
    _silero = None
    _aasist = None
    _panns = None
