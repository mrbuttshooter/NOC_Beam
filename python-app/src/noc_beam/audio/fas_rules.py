"""FAS verdict synthesis.

Takes the per-signal scores (features + model outputs + fingerprint match)
and emits a final verdict + confidence + human-readable reason list.

Verdicts:
    HUMAN_LIKELY          - live-human evidence dominates
    INCONCLUSIVE          - insufficient data
    MACHINE_OR_VOICEMAIL  - machine/recording signal, not FAS by itself
    IVR_OR_ANNOUNCEMENT   - network/menu/announcement signal, not FAS by itself
    SUSPICIOUS            - weak FAS evidence, keep watching
    PROBABLE_FAS          - multiple FAS signals or strong replay/silence evidence
    CONFIRMED_FAS         - deterministic post-answer call-progress audio

Score weights (per signal):
    fingerprint_reuse  : +3  (very high signal -- exact repeat across calls)
    ringback_after_200 : +3  (deterministic via Goertzel)
    sustained_silence  : +2
    recording_aasist   : +2
    music_on_hold      : +1
    generic_ivr        : +1
    speech_present_real: -3  (positive evidence of real call)
    vad_speech_high    : -2

Thresholds (balanced preset):
    post-answer call-progress evidence -> CONFIRMED_FAS
    score >=  5 with FAS corroboration -> PROBABLE_FAS
    score >=  2 -> SUSPICIOUS, unless the evidence is machine/IVR only
    score <= -3 with human evidence and no FAS signals -> HUMAN_LIKELY
    otherwise INCONCLUSIVE
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from noc_beam.audio.fas_evidence import FasEvidence
from noc_beam.audio.fas_features import FeatureBundle


# Per-signal weights (balanced preset). The "aggressive" / "conservative"
# presets scale the thresholds, not these.
WEIGHT_FINGERPRINT_REUSE = 3
WEIGHT_RINGBACK = 3
WEIGHT_SILENCE = 2
WEIGHT_RECORDING_AASIST = 2
WEIGHT_MOH = 1
WEIGHT_GENERIC_IVR = 1
WEIGHT_REAL_SPEECH = -3
WEIGHT_VAD_HIGH = -2

# Verdict thresholds per sensitivity preset.
PRESETS: dict[str, dict[str, float]] = {
    "conservative": {"fas": 6, "suspicious": 3},
    "balanced":     {"fas": 5, "suspicious": 2},
    "aggressive":   {"fas": 4, "suspicious": 1},
}


@dataclass
class FasVerdict:
    verdict: str
    confidence: float            # 0..1
    score: int                   # signed integer; negative = real-call evidence
    reasons: list[str] = field(default_factory=list)
    evidence: list[FasEvidence] = field(default_factory=list)

    def reasons_text(self) -> str:
        return "; ".join(self.reasons) if self.reasons else ""


def synthesise(
    *,
    features: FeatureBundle,
    silero_speech_prob: float | None,
    aasist_spoof_prob: float | None,
    panns: dict[str, float] | None,
    fingerprint_sim: float,         # 0..1, similarity to closest prior FP
    fingerprint_match: dict[str, Any] | None = None,
    fingerprint_threshold: float = 0.90,
    sensitivity: str = "balanced",
    analyzed_seconds: float = 0.0,
    sustained_silence_seconds: float = 0.0,
) -> FasVerdict:
    """Map a bag of signals to a single verdict + confidence."""
    thresholds = PRESETS.get(sensitivity, PRESETS["balanced"])
    score = 0
    reasons: list[str] = []
    evidence: list[FasEvidence] = []
    deterministic_positive = False
    positive_signal_count = 0
    machine_signal = False
    announcement_signal = False
    human_signal_count = 0

    def add_evidence(
        kind: str,
        source: str,
        weight: int,
        confidence: float,
        message: str,
        *,
        value: float | str | None = None,
        threshold: float | str | None = None,
        sticky: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        reasons.append(message)
        evidence.append(
            FasEvidence(
                kind=kind,
                source=source,
                weight=weight,
                confidence=max(0.0, min(1.0, confidence)),
                message=message,
                value=value,
                threshold=threshold,
                sticky=sticky,
                metadata=metadata or {},
            )
        )

    # ----- Positive FAS evidence -----
    if fingerprint_sim >= fingerprint_threshold:
        score += WEIGHT_FINGERPRINT_REUSE
        meta = {"scope": "fingerprint"}
        if fingerprint_match:
            meta.update(fingerprint_match)
        add_evidence(
            "audio_reuse",
            "fingerprint",
            WEIGHT_FINGERPRINT_REUSE,
            fingerprint_sim,
            f"audio matches a previous call ({fingerprint_sim:.0%})",
            value=fingerprint_sim,
            threshold=fingerprint_threshold,
            sticky=True,
            metadata=meta,
        )
        deterministic_positive = True
        positive_signal_count += 1

    # Tone/cadence detector: post-answer call-progress media is the most
    # deterministic FAS signal we can observe locally. 180/183 early media is
    # normal; this engine only runs after CONFIRMED, so ringback/busy/SIT here
    # means the supplier answered while still playing network progress audio.
    if features.tone_label in {"RINGBACK", "BUSY_OR_REORDER", "SIT_NO_SERVICE"} and features.tone_score >= 0.35:
        tone_weight = 5
        score += tone_weight
        label = {
            "RINGBACK": "ringback",
            "BUSY_OR_REORDER": "busy/reorder tone",
            "SIT_NO_SERVICE": "no-service SIT tone",
        }.get(features.tone_label, "call-progress tone")
        cadence = f", {features.tone_cadence}" if features.tone_cadence else ""
        add_evidence(
            "post_answer_call_progress",
            "tone_cadence",
            tone_weight,
            features.tone_score,
            f"{label} after answer ({features.tone_freq_hz:.0f} Hz{cadence})",
            value=features.tone_score,
            threshold=0.35,
            sticky=True,
            metadata={"scope": features.tone_label},
        )
        deterministic_positive = True
        positive_signal_count += 1
    elif features.tone_label == "CALL_PROGRESS_TONE" and features.tone_score >= 0.45:
        score += WEIGHT_RINGBACK
        add_evidence(
            "post_answer_call_progress",
            "tone_cadence",
            WEIGHT_RINGBACK,
            features.tone_score,
            f"call-progress tone after answer ({features.tone_freq_hz:.0f} Hz)",
            value=features.tone_score,
            threshold=0.45,
            sticky=True,
            metadata={"scope": features.tone_label},
        )
        deterministic_positive = True
        positive_signal_count += 1
    elif features.tone_label in {"FAX_CNG", "FAX_CED"} and features.tone_score >= 0.35:
        machine_signal = True
        add_evidence(
            "fax_machine_tone",
            "tone_cadence",
            0,
            features.tone_score,
            f"fax tone detected ({features.tone_freq_hz:.0f} Hz)",
            value=features.tone_score,
            threshold=0.35,
            metadata={"scope": features.tone_label},
        )

    # Legacy Goertzel scores on real PSTN tones tend to land 0.15..0.35 because
    # the input passes through G.711 + multiple carrier hops before
    # reaching us; the threshold is set against that range, not the
    # 0.7+ a synthetic clean sine would produce.
    if not deterministic_positive and features.ringback_score >= 0.15:
        score += WEIGHT_RINGBACK
        add_evidence(
            "post_answer_call_progress",
            "goertzel",
            WEIGHT_RINGBACK,
            min(1.0, features.ringback_score),
            f"ringback tone after answer ({features.ringback_freq_hz:.0f} Hz)",
            value=features.ringback_score,
            threshold=0.15,
            sticky=True,
            metadata={"scope": "legacy_ringback"},
        )
        deterministic_positive = True
        positive_signal_count += 1

    # Do not call early post-answer dead air suspicious by itself. Many
    # legitimate answered calls have a short silent gap before a person or
    # gateway audio starts. Treat silence as FAS evidence only when it is
    # sustained for enough confirmed-call audio.
    if sustained_silence_seconds >= 6.0 and features.silence_score >= 0.85:
        score += WEIGHT_SILENCE
        add_evidence(
            "sustained_silence",
            "energy",
            WEIGHT_SILENCE,
            features.silence_score,
            f"sustained silence ({features.silence_score:.0%} over {sustained_silence_seconds:.1f}s)",
            value=sustained_silence_seconds,
            threshold=6.0,
        )
        positive_signal_count += 1

    if aasist_spoof_prob is not None and aasist_spoof_prob >= 0.70:
        score += WEIGHT_RECORDING_AASIST
        machine_signal = True
        add_evidence(
            "recorded_or_synthetic_audio",
            "aasist",
            WEIGHT_RECORDING_AASIST,
            aasist_spoof_prob,
            f"audio detected as recorded/synthetic ({aasist_spoof_prob:.0%})",
            value=aasist_spoof_prob,
            threshold=0.70,
        )
        positive_signal_count += 1

    if panns is not None:
        if panns.get("music", 0.0) >= 0.50 and panns.get("speech", 0.0) < 0.30:
            score += WEIGHT_MOH
            announcement_signal = True
            add_evidence(
                "music_or_hold",
                "panns",
                WEIGHT_MOH,
                panns["music"],
                f"music on hold ({panns['music']:.0%})",
                value=panns["music"],
                threshold=0.50,
            )
            positive_signal_count += 1
        if panns.get("ringing", 0.0) >= 0.50:
            score += WEIGHT_GENERIC_IVR
            announcement_signal = True
            add_evidence(
                "telephony_tone_classifier",
                "panns",
                WEIGHT_GENERIC_IVR,
                panns["ringing"],
                f"telephony tone classified ({panns['ringing']:.0%})",
                value=panns["ringing"],
                threshold=0.50,
            )
            positive_signal_count += 1

    # ----- Negative (real-call) evidence -----
    if silero_speech_prob is not None and silero_speech_prob >= 0.60:
        score += WEIGHT_VAD_HIGH
        human_signal_count += 1
        add_evidence(
            "speech_present",
            "vad",
            WEIGHT_VAD_HIGH,
            silero_speech_prob,
            f"continuous speech detected ({silero_speech_prob:.0%})",
            value=silero_speech_prob,
            threshold=0.60,
        )

    # Require Silero corroboration before crediting "live conversation":
    # random noise bursts also produce many short energy runs with low
    # stability. Without VAD the rule mis-fires on canned recordings.
    if (
        features.speech_run_count >= 3
        and features.energy_stability < 0.5
        and features.silence_score < 0.3
        and silero_speech_prob is not None
        and silero_speech_prob >= 0.40
    ):
        score += WEIGHT_REAL_SPEECH
        human_signal_count += 1
        add_evidence(
            "varied_speech_pattern",
            "energy_vad",
            WEIGHT_REAL_SPEECH,
            0.75,
            "varied speech pattern (live conversation)",
            value=features.speech_run_count,
            threshold=3,
        )

    # ----- Verdict mapping -----
    if any(e.kind == "post_answer_call_progress" and e.sticky and e.confidence >= 0.35 for e in evidence):
        verdict = "CONFIRMED_FAS"
    elif score >= thresholds["fas"] and (deterministic_positive or positive_signal_count >= 2):
        verdict = "PROBABLE_FAS"
    elif machine_signal and not deterministic_positive and positive_signal_count <= 1:
        verdict = "MACHINE_OR_VOICEMAIL"
    elif announcement_signal and not deterministic_positive and positive_signal_count <= 1:
        verdict = "IVR_OR_ANNOUNCEMENT"
    elif score >= thresholds["suspicious"]:
        verdict = "SUSPICIOUS"
    elif score <= -3 and not deterministic_positive and positive_signal_count == 0 and human_signal_count > 0:
        verdict = "HUMAN_LIKELY"
    else:
        verdict = "INCONCLUSIVE"

    # Confidence: distance from "INCONCLUSIVE" centre, capped at 1.0.
    # Deterministic positives (ringback / fingerprint) pin confidence high.
    if verdict == "CONFIRMED_FAS":
        confidence = min(1.0, 0.82 + 0.03 * max(score - thresholds["fas"], 0))
    elif deterministic_positive and verdict == "PROBABLE_FAS":
        confidence = min(1.0, 0.72 + 0.05 * (score - thresholds["fas"]))
    elif verdict == "PROBABLE_FAS":
        confidence = min(1.0, 0.50 + 0.10 * (score - thresholds["fas"]))
    elif verdict == "SUSPICIOUS":
        confidence = min(0.65, 0.30 + 0.10 * (score - thresholds["suspicious"]))
    elif verdict == "HUMAN_LIKELY":
        confidence = min(0.90, 0.50 + 0.05 * (-score - 3))
    elif verdict in {"MACHINE_OR_VOICEMAIL", "IVR_OR_ANNOUNCEMENT"}:
        confidence = min(0.80, max((e.confidence for e in evidence), default=0.35))
    else:
        confidence = 0.15

    return FasVerdict(
        verdict=verdict,
        confidence=round(confidence, 3),
        score=score,
        reasons=reasons,
        evidence=evidence,
    )


def signals_summary(
    *,
    features: FeatureBundle,
    silero_speech_prob: float | None,
    aasist_spoof_prob: float | None,
    panns: dict[str, float] | None,
    fingerprint_sim: float,
) -> dict[str, Any]:
    """Diagnostic dump for CDR Detail. Not used in verdict logic."""
    return {
        "silence_score": round(features.silence_score, 3),
        "ringback_score": round(features.ringback_score, 3),
        "ringback_freq_hz": round(features.ringback_freq_hz, 1),
        "tone_label": features.tone_label,
        "tone_score": round(features.tone_score, 3),
        "tone_freq_hz": round(features.tone_freq_hz, 1),
        "tone_cadence": features.tone_cadence,
        "energy_stability": round(features.energy_stability, 3),
        "speech_run_count": features.speech_run_count,
        "rms_db": round(features.rms_db, 1),
        "silero_speech_prob": (round(silero_speech_prob, 3)
                               if silero_speech_prob is not None else None),
        "aasist_spoof_prob": (round(aasist_spoof_prob, 3)
                              if aasist_spoof_prob is not None else None),
        "panns": ({k: round(v, 3) for k, v in panns.items()}
                  if panns is not None else None),
        "fingerprint_sim": round(fingerprint_sim, 3),
    }
