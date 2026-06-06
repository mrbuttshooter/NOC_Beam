"""Unit tests for the pure-numpy FAS metrics."""
from __future__ import annotations

import math

from noc_beam.audio.fas_metrics import (
    accuracy,
    calibration_table,
    confusion_matrix,
    equal_error_rate,
    expected_calibration_error,
    per_class_metrics,
    summary,
)

LABELS = ["HUMAN_LIKELY", "PROBABLE_FAS", "INCONCLUSIVE"]


def test_confusion_matrix_counts():
    yt = ["HUMAN_LIKELY", "PROBABLE_FAS", "PROBABLE_FAS", "INCONCLUSIVE"]
    yp = ["HUMAN_LIKELY", "PROBABLE_FAS", "INCONCLUSIVE", "INCONCLUSIVE"]
    cm = confusion_matrix(yt, yp, LABELS)
    assert cm[0, 0] == 1                  # human correct
    assert cm[1, 1] == 1                  # one probable correct
    assert cm[1, 2] == 1                  # one probable -> inconclusive
    assert cm[2, 2] == 1


def test_per_class_and_accuracy():
    yt = ["PROBABLE_FAS", "PROBABLE_FAS", "HUMAN_LIKELY"]
    yp = ["PROBABLE_FAS", "HUMAN_LIKELY", "HUMAN_LIKELY"]
    assert abs(accuracy(yt, yp) - 2 / 3) < 1e-9
    m = {c.label: c for c in per_class_metrics(yt, yp, LABELS)}
    # PROBABLE_FAS: tp=1 fp=0 fn=1 -> precision 1.0, recall 0.5
    assert m["PROBABLE_FAS"].precision == 1.0
    assert m["PROBABLE_FAS"].recall == 0.5


def test_eer_perfect_separation_is_zero():
    scores = [0.9, 0.95, 0.1, 0.05]
    is_spoof = [1, 1, 0, 0]
    eer, thr = equal_error_rate(scores, is_spoof)
    assert eer < 0.01
    assert 0.1 <= thr <= 0.9


def test_eer_inverted_polarity_is_high():
    # Spoof samples scored LOW (polarity inverted) -> EER near 1.0. This is
    # the canary the corpus gate uses to catch an AASIST class-order flip.
    scores = [0.05, 0.1, 0.9, 0.95]
    is_spoof = [1, 1, 0, 0]
    eer, _ = equal_error_rate(scores, is_spoof)
    assert eer > 0.9


def test_calibration_perfect_is_zero_ece():
    # Confidence exactly matches correctness rate per bin.
    confidences = [0.95, 0.95, 0.05, 0.05]
    correct = [1, 1, 0, 0]
    ece = expected_calibration_error(confidences, correct, bins=10)
    assert ece < 0.1
    rows = calibration_table(confidences, correct, bins=10)
    assert sum(r["n"] for r in rows) == 4


def test_summary_shape():
    s = summary(["PROBABLE_FAS"], ["PROBABLE_FAS"], LABELS)
    assert s["n"] == 1
    assert s["accuracy"] == 1.0
    assert "PROBABLE_FAS" in s["per_class"]


def test_eer_degenerate_returns_nan():
    eer, thr = equal_error_rate([0.5, 0.6], [0, 0])  # no spoof samples
    assert math.isnan(eer)
