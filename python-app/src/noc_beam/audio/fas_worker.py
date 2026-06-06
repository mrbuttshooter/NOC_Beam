"""FAS inference worker thread.

A single QThread reads audio snapshots from FasAudioRouter, runs the
feature pipeline + ONNX models, synthesises a verdict via the rules
engine, and emits the result on sip_events().call_fas_verdict for Qt
to deliver on the main thread.

Schedule: per active call, score at t=4s, 8s, 13s, then every 10s.
Never downgrade past a deterministic positive signal (ringback /
fingerprint match).

The worker emits a compact verdict/reasons signal; structured evidence stays
inside the audio layer for now so the existing Qt signal remains stable.
"""
from __future__ import annotations

import logging
import time
from statistics import median

from PySide6.QtCore import QMutex, QMutexLocker, QThread, QWaitCondition

from noc_beam.audio.fas_router import fas_router
from noc_beam.sip.events import sip_events

log = logging.getLogger(__name__)

# Score-time schedule in seconds after a call enters CONFIRMED.
SCORE_TIMES_S = [4.0, 8.0, 13.0]
SCORE_INTERVAL_S = 10.0

# --- Temporal robustness knobs (CPU is not a constraint on the target box,
#     so these favour correctness over speed) ----------------------------
# AASIST k-of-n voting: a single noisy 4s frame can spike the spoof score
# and, via the sticky/monotonic lock, latch the whole call. Require
# agreement across the last N reads before the spoof signal is allowed to
# fire, and pass the median (not the peak) downstream.
AASIST_VOTE_WINDOW = 3
AASIST_VOTE_MIN_AGREE = 2
# Don't let AASIST fire on a clip that is effectively silent -- it has no
# voice to judge and codec comfort-noise can read as "synthetic".
SILENCE_GATE_SCORE = 0.85
SILENCE_GATE_RMS_DB = -45.0
# Minimum cumulative VOICED audio before any verdict above SUSPICIOUS is
# allowed to commit (deterministic positives are exempt). Stops a starved /
# silent early tick from locking PROBABLE_FAS before anyone has spoken.
MIN_VOICED_SECONDS_FOR_HIGH = 2.5
# A non-deterministic escalation must repeat across this many consecutive
# scores before it is committed (hysteresis). Deterministic positives
# escalate immediately.
ESCALATION_CONFIRM_TICKS = 2
# When de-escalation is enabled (settings.fas.allow_deescalation), the score
# must stay below the committed level for this many scores before the badge
# steps down one severity level.
DEESCALATION_PATIENCE = 3

# Verdict severity rank for monotonic locking. Higher = more severe.
# Once a verdict at rank N is committed, the badge never displays a
# rank < N for the rest of the call. Prevents the flicker (Suspicious
# -> Inconclusive -> Likely FAS) that erodes operator trust.
_SEVERITY = {
    "": 0,
    "ANALYZING": 0,
    "INCONCLUSIVE": 1,
    "LIKELY_REAL": 2,  # legacy spelling
    "HUMAN_LIKELY": 2,
    "MACHINE_OR_VOICEMAIL": 2,
    "IVR_OR_ANNOUNCEMENT": 2,
    "SUSPICIOUS": 3,
    "LIKELY_FAS": 4,  # legacy spelling
    "PROBABLE_FAS": 4,
    "CONFIRMED_FAS": 5,
}

# Per-verdict minimum confidence required to surface it on the live
# badge. Below this we keep showing the previously-committed verdict
# (or "Analyzing" if nothing committed yet). Gates can be liberal
# because the monotonic-severity lock already prevents downward
# flicker; the gate just filters out the lowest-noise readings.
# Calibrated against real echo-test traffic where SUSPICIOUS (silence)
# lands at ~0.30 and LIKELY_FAS (ringback+fingerprint) lands at 0.50+.
_MIN_CONFIDENCE_TO_SURFACE = {
    "INCONCLUSIVE": 0.0,
    "LIKELY_REAL": 0.30,
    "HUMAN_LIKELY": 0.30,
    "MACHINE_OR_VOICEMAIL": 0.45,
    "IVR_OR_ANNOUNCEMENT": 0.45,
    "SUSPICIOUS": 0.25,
    "LIKELY_FAS": 0.40,
    "PROBABLE_FAS": 0.40,
    "CONFIRMED_FAS": 0.55,
}


class _CallScoreState:
    __slots__ = ("started_at", "next_score_idx", "last_score_at", "last_verdict",
                 "last_confidence", "last_reasons", "deterministic_positive",
                 "committed_severity", "consecutive_silence_seconds",
                 "evidence_accumulator", "aasist_history", "voiced_seconds",
                 "pending_severity", "pending_count", "low_score_streak")

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.next_score_idx = 0
        self.last_score_at = 0.0
        self.last_verdict = "INCONCLUSIVE"
        self.last_confidence = 0.0
        self.last_reasons = ""
        self.deterministic_positive = False  # ringback/fingerprint locked-in
        # Monotonic verdict surface (agents' consensus): once a verdict is
        # committed it never downgrades in severity. Stored as the severity
        # rank to make comparisons trivial.
        self.committed_severity = 0  # 0=none, 1=INCONCLUSIVE, 2=LIKELY_REAL, 3=SUSPICIOUS, 4=LIKELY_FAS
        self.consecutive_silence_seconds = 0.0
        # Rolling AASIST spoof reads for k-of-n voting (most-recent last).
        self.aasist_history: list[float] = []
        # Cumulative VOICED audio heard, gating high verdicts.
        self.voiced_seconds = 0.0
        # Escalation hysteresis bookkeeping for non-deterministic verdicts.
        self.pending_severity = 0
        self.pending_count = 0
        # Consecutive low-score ticks, for optional de-escalation.
        self.low_score_streak = 0
        from noc_beam.audio.fas_evidence import FasEvidenceAccumulator

        self.evidence_accumulator = FasEvidenceAccumulator()

    def due(self, now: float) -> bool:
        elapsed = now - self.started_at
        if self.next_score_idx < len(SCORE_TIMES_S):
            return elapsed >= SCORE_TIMES_S[self.next_score_idx]
        return (now - self.last_score_at) >= SCORE_INTERVAL_S

    def mark_scored(self, now: float) -> None:
        if self.next_score_idx < len(SCORE_TIMES_S):
            self.next_score_idx += 1
        self.last_score_at = now


class FasInferenceWorker(QThread):
    """Single worker that scores every active call on a schedule.

    Owns its ONNX sessions (single-shot lazy load) and per-call state.
    """

    POLL_INTERVAL_MS = 250

    def __init__(self, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self._stop = False
        self._states: dict[int, _CallScoreState] = {}
        self._cond_mutex = QMutex()
        self._cond = QWaitCondition()

    # ------------------------------------------------------------------
    # Qt main thread API
    # ------------------------------------------------------------------
    def track(self, call_id: int) -> None:
        """Start scoring a call. Called when the call enters CONFIRMED."""
        if call_id not in self._states:
            self._states[call_id] = _CallScoreState()
        # Wake worker so it picks up the new call immediately
        with QMutexLocker(self._cond_mutex):
            self._cond.wakeAll()

    def untrack(self, call_id: int) -> None:
        """Stop scoring a call. Called on DISCONNECTED."""
        self._states.pop(call_id, None)

    def request_stop(self) -> None:
        self._stop = True
        with QMutexLocker(self._cond_mutex):
            self._cond.wakeAll()

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------
    def run(self) -> None:  # noqa: D401
        log.info("FasInferenceWorker started")
        try:
            while not self._stop:
                now = time.monotonic()
                # Snapshot the keys -- can't mutate dict mid-iteration.
                for call_id in list(self._states.keys()):
                    state = self._states.get(call_id)
                    if state is None:
                        continue
                    if not state.due(now):
                        continue
                    self._score_one(call_id, state)
                    state.mark_scored(now)

                # Sleep until next poll or wake
                with QMutexLocker(self._cond_mutex):
                    self._cond.wait(self._cond_mutex, self.POLL_INTERVAL_MS)
        finally:
            log.info("FasInferenceWorker stopped")

    # ------------------------------------------------------------------
    # Per-call scoring
    # ------------------------------------------------------------------
    def _sensitivity(self) -> str:
        # Read settings lazily; if anything fails fall back to balanced.
        try:
            from noc_beam.config.store import load_settings

            cfg = load_settings()
            return getattr(getattr(cfg, "fas", None), "sensitivity", "balanced") or "balanced"
        except Exception:
            return "balanced"

    def _allow_deescalation(self) -> bool:
        try:
            from noc_beam.config.store import load_settings

            cfg = load_settings()
            return bool(getattr(getattr(cfg, "fas", None), "allow_deescalation", False))
        except Exception:
            return False

    def _score_one(self, call_id: int, state: _CallScoreState) -> None:
        from noc_beam.audio.fas_features import extract_features
        from noc_beam.audio.fas_fingerprint import fingerprint_clip, fingerprint_memory
        from noc_beam.audio.fas_models import aasist_detector, panns_classifier, silero_vad
        from noc_beam.audio.fas_rules import synthesise

        from noc_beam.audio.fas_tap import FAS_SAMPLE_RATE

        router = fas_router()
        clip = router.snapshot(call_id, seconds=4.0)
        total_samples = router.total_samples(call_id)
        active = router.active_calls()
        log.info(
            "FAS score tick call=%s total_samples=%d clip=%d active_calls=%s",
            call_id, total_samples, clip.size, active,
        )

        # Need at least 2 seconds of answered-call audio to attempt a verdict. The
        # AudioMediaRecorder writes WAV at the bridge's native rate
        # (16 kHz on this PJSIP build); FAS_SAMPLE_RATE mirrors that.
        min_samples = FAS_SAMPLE_RATE * 2
        if clip.size == 0 or total_samples < min_samples:
            verdict_obj = None
            verdict, confidence, reasons = "ANALYZING", 0.0, "warming up"
        else:
            # Pass the router's native rate (matches the port's negotiated
            # codec rate). Feature extractors and model wrappers handle
            # resampling to their expected rates internally.
            features = extract_features(clip, sample_rate=FAS_SAMPLE_RATE)
            window_seconds = clip.size / float(FAS_SAMPLE_RATE)
            is_silent = (
                features.silence_score >= SILENCE_GATE_SCORE
                and features.rms_db <= SILENCE_GATE_RMS_DB
            )
            if features.silence_score >= 0.85:
                if state.last_score_at > 0:
                    elapsed_since_last_score = max(0.0, time.monotonic() - state.last_score_at)
                    state.consecutive_silence_seconds += min(window_seconds, elapsed_since_last_score)
                else:
                    state.consecutive_silence_seconds = window_seconds
            else:
                state.consecutive_silence_seconds = 0.0

            # Track cumulative VOICED audio (used to gate high verdicts). A
            # window only counts as voiced when it isn't dominated by silence.
            if features.silence_score < SILENCE_GATE_SCORE:
                if state.last_score_at > 0:
                    elapsed = max(0.0, time.monotonic() - state.last_score_at)
                    state.voiced_seconds += min(window_seconds, elapsed)
                else:
                    state.voiced_seconds += window_seconds

            # ONNX models -- each returns None if unavailable; rules
            # engine tolerates None for every signal independently.
            silero_p = silero_vad().score(clip, sample_rate=FAS_SAMPLE_RATE)
            # Silence-gate AASIST: no voice to judge -> no spoof read.
            aasist_raw = (
                None if is_silent
                else aasist_detector().score(clip, sample_rate=FAS_SAMPLE_RATE)
            )
            # PANNs is trained on ~10s clips and reads noisy on 4s; feed it the
            # full rolling window (already buffered) instead of the 4s clip.
            panns_clip = router.snapshot(call_id, seconds=10.0)
            panns_out = panns_classifier().score(
                panns_clip if panns_clip.size else clip, sample_rate=FAS_SAMPLE_RATE
            )

            # k-of-n temporal voting on AASIST. A lone high read must not set
            # machine_signal and latch the call; require AASIST_VOTE_MIN_AGREE
            # of the last AASIST_VOTE_WINDOW reads above threshold, and pass
            # the MEDIAN downstream (robust to a single outlier).
            from noc_beam.audio.fas_rules import AASIST_SPOOF_THRESHOLD

            if aasist_raw is not None:
                state.aasist_history.append(float(aasist_raw))
                if len(state.aasist_history) > AASIST_VOTE_WINDOW:
                    del state.aasist_history[:-AASIST_VOTE_WINDOW]
            recent = state.aasist_history
            if not recent:
                aasist_p = None
            elif len(recent) >= AASIST_VOTE_MIN_AGREE:
                above = sum(1 for p in recent if p >= AASIST_SPOOF_THRESHOLD)
                if above >= AASIST_VOTE_MIN_AGREE:
                    aasist_p = float(median(recent))          # corroborated -> may fire
                else:
                    aasist_p = float(min(recent))             # below threshold -> won't fire
            else:
                # Only one read so far: never enough to lock on its own. Pass it
                # through but clamped below the firing bar.
                aasist_p = min(recent[-1], AASIST_SPOOF_THRESHOLD - 0.01)

            # Fingerprint matching scoped by account_id when available.
            meta = router.meta(call_id)
            account_id = meta.get("account_id", "")
            supplier = meta.get("supplier", "")
            fp = fingerprint_clip(clip, sample_rate=FAS_SAMPLE_RATE)
            fp_sim = 0.0
            entry = None
            if fp:
                fp_sim, entry = fingerprint_memory().match(
                    fp, call_id=call_id, account_id=account_id, supplier=supplier,
                )
                fingerprint_memory().add(
                    fp, call_id=call_id, account_id=account_id, supplier=supplier,
                )

            # Carry only the non-PII signal that a prior match existed.
            # Earlier versions passed matched_call_id / matched_account_id /
            # matched_supplier through into FasEvidence.metadata, which is
            # persisted and exported in CSV -- a cross-call PII leak.
            fingerprint_match = {"prior_match": True} if entry is not None else None

            verdict_obj = synthesise(
                features=features,
                silero_speech_prob=silero_p,
                aasist_spoof_prob=aasist_p,
                panns=panns_out,
                fingerprint_sim=fp_sim,
                fingerprint_match=fingerprint_match,
                sensitivity=self._sensitivity(),
                analyzed_seconds=total_samples / float(FAS_SAMPLE_RATE),
                sustained_silence_seconds=state.consecutive_silence_seconds,
            )
            state.evidence_accumulator.add_many(verdict_obj.evidence)
            verdict = verdict_obj.verdict
            confidence = verdict_obj.confidence
            accumulated_reasons = state.evidence_accumulator.reasons_text()
            reasons = accumulated_reasons or verdict_obj.reasons_text()

            # Lock in deterministic positives so the next score interval
            # doesn't downgrade past a fingerprint / ringback trigger.
            if state.evidence_accumulator.has_sticky_positive():
                state.deterministic_positive = True

            # Minimum-voiced-budget gate: don't let a starved/silent early
            # tick commit a verdict above SUSPICIOUS before we've actually
            # heard enough voice. Deterministic positives (ringback /
            # fingerprint) are certain on their own and bypass this.
            if (
                not state.deterministic_positive
                and _SEVERITY.get(verdict, 0) > _SEVERITY["SUSPICIOUS"]
                and state.voiced_seconds < MIN_VOICED_SECONDS_FOR_HIGH
            ):
                verdict = "SUSPICIOUS"
                confidence = min(confidence, 0.45)
                hold_reason = "awaiting more voice before a firm verdict"
                reasons = f"{reasons}; {hold_reason}" if reasons else hold_reason

        # ----- Confidence gate ---------------------------------------
        # If the raw verdict doesn't clear its confidence floor, fall
        # back to whatever we last committed. New calls with no prior
        # commit show "ANALYZING" until something clears.
        min_conf = _MIN_CONFIDENCE_TO_SURFACE.get(verdict, 0.0)
        if confidence < min_conf and state.committed_severity == 0:
            verdict, confidence, reasons = "ANALYZING", 0.0, "gathering evidence"
        elif confidence < min_conf:
            # Stay with previously committed verdict, don't expose
            # the low-confidence reading.
            verdict = state.last_verdict
            confidence = state.last_confidence
            reasons = state.last_reasons

        # ----- Severity lock with hysteresis -------------------------
        # Escalations to a higher severity must be CONFIRMED across
        # ESCALATION_CONFIRM_TICKS consecutive scores so a single noisy
        # frame can't latch the call; deterministic positives (ringback /
        # fingerprint / CONFIRMED) escalate immediately. Downgrades are
        # blocked by default (monotonic), but may be accepted after a
        # sustained low-score streak when de-escalation is enabled.
        new_sev = _SEVERITY.get(verdict, 0)
        is_det = state.deterministic_positive or new_sev >= _SEVERITY["CONFIRMED_FAS"]

        if new_sev > state.committed_severity:
            state.low_score_streak = 0
            if is_det:
                state.committed_severity = new_sev
                state.pending_severity = 0
                state.pending_count = 0
            else:
                if state.pending_severity == new_sev:
                    state.pending_count += 1
                else:
                    state.pending_severity = new_sev
                    state.pending_count = 1
                if state.pending_count >= ESCALATION_CONFIRM_TICKS:
                    state.committed_severity = new_sev
                    state.pending_severity = 0
                    state.pending_count = 0
                else:
                    # Escalation not yet confirmed: hold the prior surface.
                    verdict = state.last_verdict or "ANALYZING"
                    confidence = state.last_confidence
                    reasons = state.last_reasons
        elif new_sev < state.committed_severity:
            state.pending_severity = 0
            state.pending_count = 0
            state.low_score_streak += 1
            if (
                self._allow_deescalation()
                and not state.deterministic_positive
                and state.low_score_streak >= DEESCALATION_PATIENCE
            ):
                # Sustained low score: accept the downgrade and surface it.
                state.committed_severity = new_sev
                state.low_score_streak = 0
            else:
                # Default monotonic behaviour: keep the committed verdict.
                verdict = state.last_verdict
                confidence = max(confidence, state.last_confidence)
                reasons = state.last_reasons
        else:
            # Steady state at the committed severity.
            state.pending_severity = 0
            state.pending_count = 0
            state.low_score_streak = 0

        if state.deterministic_positive and _SEVERITY.get(verdict, 0) < _SEVERITY["SUSPICIOUS"]:
            # Belt-and-suspenders: deterministic-positive signals
            # (ringback, fingerprint reuse) lock in at SUSPICIOUS minimum.
            verdict = state.last_verdict
            confidence = state.last_confidence
            reasons = state.last_reasons

        state.last_verdict = verdict
        state.last_confidence = confidence
        state.last_reasons = reasons

        log.info(
            "FAS verdict call=%s final=%s conf=%.2f committed_sev=%d reasons=%s",
            call_id, verdict, confidence, state.committed_severity, reasons,
        )

        try:
            sip_events().call_fas_verdict.emit(call_id, verdict, confidence, reasons)
        except Exception:
            log.exception("Failed to emit call_fas_verdict for call %s", call_id)


_worker: FasInferenceWorker | None = None


def fas_worker() -> FasInferenceWorker:
    global _worker
    if _worker is None:
        _worker = FasInferenceWorker()
    return _worker


def shutdown_fas_worker() -> None:
    """Stop and join the worker, if one was started."""
    global _worker
    if _worker is None:
        return
    _worker.request_stop()
    if not _worker.wait(3000):
        log.warning("FasInferenceWorker did not stop within 3s; terminating")
        _worker.terminate()
        _worker.wait(1000)
    _worker = None
