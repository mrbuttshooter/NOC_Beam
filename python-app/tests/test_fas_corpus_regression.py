"""FAS corpus regression gate + corpus-tooling coverage.

Three layers:
  * schema test (always runs) -- the example manifest parses + has the keys.
  * builder round-trip (always runs) -- labelled_from_db / scaffold_from_clips
    work on a tiny temp DB + WAV.
  * regression gate (skips unless a labelled manifest AND the ONNX models are
    present) -- replays each labelled WAV through the live path and checks
    accuracy against baseline.json (or a floor when none is frozen yet).

The gate skips cleanly on CI machines without models, so it only enforces on
a box where the corpus + models actually exist (e.g. the Windows build host).
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import wave
from pathlib import Path

import numpy as np
import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_CORPUS_DIR = _HERE / "fas_corpus"
_EXAMPLE = _CORPUS_DIR / "manifest.example.jsonl"
_MANIFEST = _CORPUS_DIR / "manifest.jsonl"
_BASELINE = _CORPUS_DIR / "baseline.json"

_REQUIRED_KEYS = {"sha256", "basename", "label", "source"}
# Verdict labels that count as ground-truth (UNLABELED is a scaffold).
_VALID_LABELS = {
    "HUMAN_LIKELY", "INCONCLUSIVE", "MACHINE_OR_VOICEMAIL",
    "IVR_OR_ANNOUNCEMENT", "SUSPICIOUS", "PROBABLE_FAS", "CONFIRMED_FAS",
}


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "export_corpus", _REPO / "build" / "export_corpus_from_sweepdb.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- schema (always runs) -----------------------------------------------
def test_example_manifest_parses():
    lines = [l for l in _EXAMPLE.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines
    for line in lines:
        entry = json.loads(line)
        assert _REQUIRED_KEYS <= set(entry), f"missing keys in {entry}"
        # PII discipline: no raw destination number anywhere.
        assert "destination_e164" not in entry
        assert "destination" not in entry


# ---- builder round-trip (always runs) -----------------------------------
def _write_wav(path: Path, seconds: float = 3.0) -> None:
    n = int(16000 * seconds)
    clip = (np.sin(2 * np.pi * 440 * np.arange(n) / 16000) * 0.3 * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(clip.tobytes())


def test_builder_extracts_labelled_and_scaffold(tmp_path):
    mod = _load_builder()
    # Temp DB with one labelled call pointing at a real wav.
    wav = tmp_path / "labelled.wav"
    _write_wav(wav)
    db = tmp_path / "fas_sweep.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE calls (call_id INTEGER PRIMARY KEY, supplier_id TEXT, "
        "destination_e164 TEXT, duration_s REAL, sip_final_code INTEGER, "
        "fas_verdict TEXT, fas_score INTEGER, fas_reasons TEXT, wav_path TEXT);"
    )
    conn.execute(
        "INSERT INTO calls(supplier_id,destination_e164,duration_s,sip_final_code,"
        "fas_verdict,fas_score,fas_reasons,wav_path) VALUES(?,?,?,?,?,?,?,?)",
        ("supA", "0123456789", 30.0, 200, "CONFIRMED_FAS", 8, "ringback", str(wav)),
    )
    conn.commit()
    conn.close()

    labelled = mod.labelled_from_db(db)
    assert len(labelled) == 1
    e = labelled[0]
    assert e["label"] == "CONFIRMED_FAS"
    assert e["source"] == "sweepdb"
    assert len(e["sha256"]) == 64
    assert "destination_e164" not in e        # PII stripped
    assert e["supplier_bucket"] and len(e["supplier_bucket"]) == 8

    # A loose clip dir -> scaffold, deduped against the labelled sha.
    clips = tmp_path / "clips"
    clips.mkdir()
    _write_wav(clips / "loose.wav", seconds=4.0)
    scaffold = mod.scaffold_from_clips(clips, {e["sha256"]})
    assert len(scaffold) == 1
    assert scaffold[0]["label"] == "UNLABELED"


# ---- regression gate (conditional) --------------------------------------
def _labelled_entries() -> list[dict]:
    if not _MANIFEST.exists():
        return []
    out = []
    for line in _MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if e.get("label") in _VALID_LABELS:
            out.append(e)
    return out


def test_corpus_regression_gate(tmp_path):
    import os

    entries = _labelled_entries()
    if not entries:
        pytest.skip("no labelled corpus yet (manifest.jsonl absent or all UNLABELED)")

    from noc_beam.audio.fas_models import silero_vad
    if not silero_vad().available:
        pytest.skip("ONNX models not bundled on this machine")

    from noc_beam.audio.fas_metrics import accuracy
    from noc_beam.audio.fas_replay import replay_wav

    clip_dir = Path(os.environ.get("FAS_CORPUS_DIR", "")) if os.environ.get("FAS_CORPUS_DIR") else None
    if clip_dir is None:
        try:
            from noc_beam.config.paths import data_dir
            clip_dir = data_dir() / "fas_clips"
        except Exception:
            pytest.skip("no clip directory resolvable")
    if not clip_dir.exists():
        pytest.skip(f"clip dir {clip_dir} not present")

    y_true, y_pred = [], []
    for e in entries:
        wav = clip_dir / e["basename"]
        if not wav.exists():
            continue
        y_true.append(e["label"])
        y_pred.append(replay_wav(wav).final_verdict)

    if not y_true:
        pytest.skip("labelled entries reference clips not on this machine")

    acc = accuracy(y_true, y_pred)
    floor = 0.0
    if _BASELINE.exists():
        floor = float(json.loads(_BASELINE.read_text())["accuracy"]) - 0.05
    assert acc >= floor, f"FAS accuracy {acc:.3f} regressed below baseline {floor:.3f}"
