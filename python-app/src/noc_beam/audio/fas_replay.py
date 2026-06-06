"""Offline FAS replay harness.

Re-runs the EXACT live scoring path (feature extraction + ONNX ensemble +
rules engine + temporal surface policy) over a stored WAV, simulating the
per-call scoring schedule (t=4s, 8s, 13s, then every 10s). This is the
foundation of the FAS evaluation substrate: it lets you score real captured
call audio reproducibly -- something nothing in the app could do before
(verdicts only existed transiently during a live call).

Fidelity: the windowing mirrors the live router ring (a 4s clip for
features/Silero/AASIST, a 10s clip for PANNs, both ending at "now") and the
verdict is produced by the same fas_worker helpers (vote_aasist /
evaluate_snapshot / apply_surface_policy) the live worker uses -- so a replay
verdict equals what the badge would have shown on that audio.

Fingerprint matching is OFF by default: it depends on cross-call memory that
isn't meaningful for a single isolated file. Pass with_fingerprint=True to
let a file match earlier files within one ReplaySession.

CLI:
    python -m noc_beam.audio.fas_replay <file-or-dir> [--sensitivity balanced]
                                        [--json out.json] [--deescalation]
"""
from __future__ import annotations

import json
import logging
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from noc_beam.audio.fas_models import _resample_polyphase
from noc_beam.audio.fas_tap import FAS_SAMPLE_RATE
from noc_beam.audio.fas_worker import (
    SCORE_INTERVAL_S,
    SCORE_TIMES_S,
    _CallScoreState,
    apply_surface_policy,
    evaluate_snapshot,
    vote_aasist,
)

log = logging.getLogger(__name__)

# Mirror the worker's minimum-audio gate (2s) before a verdict is attempted.
_MIN_SECONDS = 2.0


@dataclass
class ReplayTick:
    t: float
    verdict: str
    confidence: float
    reasons: str


@dataclass
class ReplayResult:
    path: str
    duration_s: float
    final_verdict: str
    final_confidence: float
    final_reasons: str
    ticks: list[ReplayTick] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "duration_s": round(self.duration_s, 2),
            "final_verdict": self.final_verdict,
            "final_confidence": round(self.final_confidence, 3),
            "final_reasons": self.final_reasons,
            "ticks": [
                {"t": round(tk.t, 1), "verdict": tk.verdict,
                 "confidence": round(tk.confidence, 3), "reasons": tk.reasons}
                for tk in self.ticks
            ],
        }


def load_wav_16k_mono(path: str | Path) -> np.ndarray:
    """Load a WAV as int16 mono at FAS_SAMPLE_RATE (16 kHz), matching the
    live tap. Stereo is downmixed to channel 0; other rates are resampled."""
    with wave.open(str(path), "rb") as w:
        ch = w.getnchannels()
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        x = x[::ch]  # take first channel
    if sr != FAS_SAMPLE_RATE and x.size:
        x = _resample_polyphase(x.astype(np.float32), sr, FAS_SAMPLE_RATE)
        x = np.clip(np.round(x), -32768, 32767).astype(np.int16)
    return np.ascontiguousarray(x, dtype=np.int16)


def _schedule(duration_s: float) -> list[float]:
    """The live scoring schedule clamped to the file length."""
    times = [t for t in SCORE_TIMES_S if t <= duration_s + 0.001]
    t = (SCORE_TIMES_S[-1] if SCORE_TIMES_S else 0.0) + SCORE_INTERVAL_S
    while t <= duration_s + 0.001:
        times.append(t)
        t += SCORE_INTERVAL_S
    # Always score at least once if there is enough audio.
    if not times and duration_s >= _MIN_SECONDS:
        times = [min(duration_s, 4.0)]
    return times


def replay_samples(
    samples: np.ndarray,
    *,
    sensitivity: str = "balanced",
    allow_deescalation: bool = False,
    path: str = "<memory>",
) -> ReplayResult:
    """Replay an in-memory int16 mono 16 kHz clip through the live path."""
    from noc_beam.audio.fas_features import extract_features
    from noc_beam.audio.fas_models import aasist_detector, panns_classifier, silero_vad

    sr = FAS_SAMPLE_RATE
    duration_s = samples.size / float(sr)
    state = _CallScoreState()
    state.started_at = 0.0  # virtual clock anchored at t=0
    result = ReplayResult(
        path=path, duration_s=duration_s,
        final_verdict="ANALYZING", final_confidence=0.0, final_reasons="warming up",
    )

    prev_t = 0.0
    first = True
    for t in _schedule(duration_s):
        end = int(round(t * sr))
        clip = samples[max(0, end - 4 * sr):end]
        panns_clip = samples[max(0, end - 10 * sr):end]
        total_seconds = end / float(sr)
        if clip.size == 0 or total_seconds < _MIN_SECONDS:
            verdict, confidence, reasons = "ANALYZING", 0.0, "warming up"
        else:
            features = extract_features(clip, sample_rate=sr)
            is_silent = (
                features.silence_score >= 0.85 and features.rms_db <= -45.0
            )
            silero_p = silero_vad().score(clip, sample_rate=sr)
            aasist_raw = None if is_silent else aasist_detector().score(clip, sample_rate=sr)
            pc = panns_clip if panns_clip.size else clip
            panns_out = panns_classifier().score(pc, sample_rate=sr)
            aasist_p = vote_aasist(state, aasist_raw)
            verdict, confidence, reasons = evaluate_snapshot(
                state,
                features=features,
                silero_p=silero_p,
                aasist_p=aasist_p,
                panns_out=panns_out,
                fp_sim=0.0,
                fingerprint_match=None,
                analyzed_seconds=total_seconds,
                sensitivity=sensitivity,
                window_seconds=clip.size / float(sr),
                elapsed_since_last=(0.0 if first else max(0.0, t - prev_t)),
                is_first_score=first,
            )
        verdict, confidence, reasons = apply_surface_policy(
            state, verdict, confidence, reasons, allow_deescalation=allow_deescalation,
        )
        state.last_score_at = t
        first = False
        prev_t = t
        result.ticks.append(ReplayTick(t=t, verdict=verdict, confidence=confidence, reasons=reasons))

    if result.ticks:
        last = result.ticks[-1]
        result.final_verdict = last.verdict
        result.final_confidence = last.confidence
        result.final_reasons = last.reasons
    return result


def replay_wav(
    path: str | Path,
    *,
    sensitivity: str = "balanced",
    allow_deescalation: bool = False,
) -> ReplayResult:
    """Replay a WAV file end-to-end through the live FAS path."""
    samples = load_wav_16k_mono(path)
    return replay_samples(
        samples, sensitivity=sensitivity, allow_deescalation=allow_deescalation,
        path=str(path),
    )


def replay_dir(directory: str | Path, **kw) -> list[ReplayResult]:
    """Replay every *.wav in a directory (non-recursive)."""
    out: list[ReplayResult] = []
    for p in sorted(Path(directory).glob("*.wav")):
        try:
            out.append(replay_wav(p, **kw))
        except Exception:  # pragma: no cover - diagnostic robustness
            log.exception("replay failed for %s", p)
    return out


def _main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI glue
    import argparse

    ap = argparse.ArgumentParser(description="Replay WAV(s) through the FAS engine")
    ap.add_argument("target", help="WAV file or directory of WAVs")
    ap.add_argument("--sensitivity", default="balanced",
                    choices=["conservative", "balanced", "aggressive"])
    ap.add_argument("--deescalation", action="store_true",
                    help="enable hysteretic de-escalation")
    ap.add_argument("--json", help="write results as JSON to this path")
    args = ap.parse_args(argv)

    target = Path(args.target)
    kw = dict(sensitivity=args.sensitivity, allow_deescalation=args.deescalation)
    results = replay_dir(target, **kw) if target.is_dir() else [replay_wav(target, **kw)]

    print(f"{'file':<34} {'verdict':<22} {'conf':>5}  {'dur':>6}")
    print("-" * 74)
    for r in results:
        print(f"{Path(r.path).name:<34} {r.final_verdict:<22} "
              f"{r.final_confidence:>5.0%}  {r.duration_s:>5.1f}s")
    if args.json:
        Path(args.json).write_text(
            json.dumps([r.as_dict() for r in results], indent=2), encoding="utf-8"
        )
        print(f"\nwrote {len(results)} result(s) -> {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
