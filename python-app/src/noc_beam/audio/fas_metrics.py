"""FAS evaluation metrics (pure numpy -- no sklearn, keeps the bundle lean).

Turns a set of (predicted verdict, true label) pairs and AASIST
spoof-scores into the numbers needed to prove the verdict improved or
regressed:

    * confusion_matrix / per-class precision-recall-F1  -> classification quality
    * equal_error_rate                                  -> AASIST polarity+threshold guard
    * calibration_table                                 -> "is a 0.7 confidence actually 70% right?"

These power the offline corpus regression gate (tests/test_fas_corpus_*),
so a silent accuracy drop becomes a red CI run instead of a field surprise.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ClassMetrics:
    label: str
    precision: float
    recall: float
    f1: float
    support: int


def confusion_matrix(
    y_true: list[str], y_pred: list[str], labels: list[str]
) -> np.ndarray:
    """Rows = true label, cols = predicted label, in `labels` order."""
    idx = {l: i for i, l in enumerate(labels)}
    m = np.zeros((len(labels), len(labels)), dtype=np.int64)
    for t, p in zip(y_true, y_pred):
        if t in idx and p in idx:
            m[idx[t], idx[p]] += 1
    return m


def per_class_metrics(
    y_true: list[str], y_pred: list[str], labels: list[str]
) -> list[ClassMetrics]:
    cm = confusion_matrix(y_true, y_pred, labels)
    out: list[ClassMetrics] = []
    for i, label in enumerate(labels):
        tp = int(cm[i, i])
        fp = int(cm[:, i].sum() - tp)
        fn = int(cm[i, :].sum() - tp)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if (precision + recall) else 0.0)
        out.append(ClassMetrics(label, precision, recall, f1, int(cm[i, :].sum())))
    return out


def accuracy(y_true: list[str], y_pred: list[str]) -> float:
    if not y_true:
        return 0.0
    return sum(1 for t, p in zip(y_true, y_pred) if t == p) / len(y_true)


def equal_error_rate(scores: list[float], is_spoof: list[int]) -> tuple[float, float]:
    """EER for a binary spoof detector.

    `scores` = P(spoof) per sample, `is_spoof` = 1 for spoof else 0.
    Returns (eer, threshold). EER is the operating point where the false-
    accept rate (spoof passed as bonafide) equals the false-reject rate
    (bonafide flagged spoof). A polarity inversion drives EER toward ~1.0,
    so this is the canary for the AASIST class-order bug.
    """
    s = np.asarray(scores, dtype=np.float64)
    y = np.asarray(is_spoof, dtype=np.int64)
    if s.size == 0 or y.sum() == 0 or (y == 0).sum() == 0:
        return float("nan"), float("nan")
    thresholds = np.unique(np.concatenate(([0.0, 1.0], s)))
    best_gap = float("inf")
    best_eer, best_thr = 1.0, 0.5
    for thr in thresholds:
        pred = s >= thr                                  # predict spoof when >= thr
        far = float(np.sum(pred[y == 0]) / np.sum(y == 0))   # bonafide flagged spoof
        frr = float(np.sum(~pred[y == 1]) / np.sum(y == 1))  # spoof passed as bonafide
        gap = abs(far - frr)
        if gap < best_gap:
            best_gap = gap
            best_eer = (far + frr) / 2.0
            best_thr = float(thr)
    return float(best_eer), float(best_thr)


def calibration_table(
    confidences: list[float], correct: list[int], bins: int = 10
) -> list[dict]:
    """Reliability bins: for confidences falling in each [lo,hi) bin, the
    mean predicted confidence vs the empirical fraction correct. A well-
    calibrated verdict has mean_confidence ~= accuracy in every bin."""
    c = np.asarray(confidences, dtype=np.float64)
    ok = np.asarray(correct, dtype=np.float64)
    edges = np.linspace(0.0, 1.0, bins + 1)
    rows: list[dict] = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (c >= lo) & (c < hi if i < bins - 1 else c <= hi)
        n = int(mask.sum())
        rows.append({
            "bin": f"[{lo:.1f},{hi:.1f})",
            "n": n,
            "mean_confidence": float(c[mask].mean()) if n else 0.0,
            "accuracy": float(ok[mask].mean()) if n else 0.0,
        })
    return rows


def expected_calibration_error(confidences: list[float], correct: list[int], bins: int = 10) -> float:
    """Single-number calibration: weighted |confidence - accuracy| over bins."""
    rows = calibration_table(confidences, correct, bins)
    total = sum(r["n"] for r in rows) or 1
    return float(sum(r["n"] * abs(r["mean_confidence"] - r["accuracy"]) for r in rows) / total)


def summary(
    y_true: list[str], y_pred: list[str], labels: list[str]
) -> dict:
    """One-call rollup for reports / CI baselines."""
    return {
        "n": len(y_true),
        "accuracy": round(accuracy(y_true, y_pred), 4),
        "per_class": {
            m.label: {
                "precision": round(m.precision, 4),
                "recall": round(m.recall, 4),
                "f1": round(m.f1, 4),
                "support": m.support,
            }
            for m in per_class_metrics(y_true, y_pred, labels)
        },
    }
