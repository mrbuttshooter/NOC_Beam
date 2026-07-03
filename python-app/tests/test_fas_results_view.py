"""Unit tests for :class:`noc_beam.ui.fas_results_view.FasResultsView`.

Constructs the widget against a tmp_path-backed sqlite db, drives a few
rows in, and exercises the filter / detail / CSV-export / ZIP-export
plumbing. Audio playback is NOT exercised end-to-end (it would require
QtMultimedia + a working backend) -- we only assert the widget exists
and gates correctly.
"""
from __future__ import annotations

import csv
import io
import os
import wave
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QApplication = QtWidgets.QApplication
_APP = QApplication.instance()
if _APP is None:
    _APP = QApplication([])

from noc_beam.audio.fas_sweep_db import FasSweepDb  # noqa: E402
from noc_beam.ui.fas_results_view import (  # noqa: E402
    COL_DEST,
    COL_SCORE,
    COL_SUPPLIER,
    COL_VERDICT,
    CSV_HEADER,
    FasResultsView,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture()
def qt_app() -> QApplication:
    return _APP


def _write_silent_wav(path: Path, seconds: float = 0.1, sample_rate: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(sample_rate * seconds)
    samples = np.zeros(n, dtype=np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())
    return path


@pytest.fixture()
def populated_db(tmp_path: Path) -> tuple[FasSweepDb, str, list[Path]]:
    db = FasSweepDb(tmp_path / "sweep.db")
    run_id = db.open_run(mode="fas-sweep", tries_per_pair=2, notes="unit-test")

    wavs: list[Path] = []
    for i, (supplier, dest, verdict, score, reasons) in enumerate([
        ("S001", "+15551234567", "CONFIRMED_FAS",       8,  ["ringback_after_200", "audio_reuse"]),
        ("S001", "+15551234567", "CONFIRMED_FAS",       7,  ["ringback_after_200"]),
        ("S002", "+15551234567", "SUSPICIOUS",          3,  ["sustained_silence"]),
        ("S003", "+447700900000", "HUMAN_LIKELY",      -4,  []),
        ("S004", "+447700900000", "INCONCLUSIVE",       0,  []),
        ("S005", "+447700900000", "MACHINE_OR_VOICEMAIL", 2, ["recording_aasist"]),
        ("S006", "+33150000000",  "FAIL_TIMEOUT",       None, ["sip_487_after_30s"]),
    ]):
        wav = _write_silent_wav(tmp_path / "wavs" / f"call_{i:03d}.wav")
        wavs.append(wav)
        db.record_call(
            run_id=run_id,
            supplier_id=supplier,
            destination_e164=dest,
            try_idx=i % 2,
            started_at=datetime(2026, 5, 25, 14, 30, i),
            duration_s=1.5 + i,
            sip_final_code=200 if verdict != "FAIL_TIMEOUT" else None,
            fas_verdict=verdict,
            fas_score=score,
            fas_reasons=reasons,
            wav_path=wav,
        )
    db.close_run(run_id)
    return db, run_id, wavs


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------
def test_constructs_against_empty_db(qt_app: QApplication, tmp_path: Path) -> None:
    db = FasSweepDb(tmp_path / "empty.db")
    view = FasResultsView(db)
    assert view.table.rowCount() == 0
    assert view.current_run_id() == ""
    assert "No run selected" in view.header_label.text()


def test_load_run_emits_run_changed_signal(
    qt_app: QApplication,
    populated_db: tuple[FasSweepDb, str, list[Path]],
) -> None:
    db, run_id, _ = populated_db
    view = FasResultsView(db)
    received: list[str] = []
    view.run_changed.connect(received.append)
    view.load_run(run_id)
    assert received == [run_id]
    # Reloading the same run id should NOT re-emit (signal is fired only
    # on actual change so subscribers don't churn).
    view.load_run(run_id)
    assert received == [run_id]


# ----------------------------------------------------------------------
# Table population
# ----------------------------------------------------------------------
def test_populates_table_from_run(
    qt_app: QApplication, populated_db: tuple[FasSweepDb, str, list[Path]]
) -> None:
    db, run_id, _ = populated_db
    view = FasResultsView(db)
    view.load_run(run_id)
    assert view.table.rowCount() == 7
    # Every expected supplier appears at least once in the table.
    suppliers = {view.table.item(r, COL_SUPPLIER).text() for r in range(7)}
    assert {"S001", "S002", "S003", "S004", "S005", "S006"} <= suppliers
    # Header reflects totals + FAS count.
    header_text = view.header_label.text()
    assert run_id in header_text
    assert "2 CONFIRMED_FAS" in header_text


def test_filter_chip_narrows_visible_rows(
    qt_app: QApplication, populated_db: tuple[FasSweepDb, str, list[Path]]
) -> None:
    db, run_id, _ = populated_db
    view = FasResultsView(db)
    view.load_run(run_id)
    assert view.table.rowCount() == 7

    # Programmatic toggle: click the CONFIRMED_FAS chip.
    view._set_filter("CONFIRMED_FAS")
    assert view.table.rowCount() == 2
    for r in range(view.table.rowCount()):
        v = view.table.item(r, COL_VERDICT).text()
        assert "Confirmed FAS" in v

    # SUSPICIOUS -> exactly one row.
    view._set_filter("SUSPICIOUS")
    assert view.table.rowCount() == 1

    # FAIL_TIMEOUT bucket reachable from the chip set too.
    view._set_filter("FAIL_TIMEOUT")
    assert view.table.rowCount() == 1
    assert view.table.item(0, COL_SCORE).text() == ""  # no score for fail rows

    # Back to All restores everything.
    view._set_filter(None)
    assert view.table.rowCount() == 7


# ----------------------------------------------------------------------
# Audio player gating
# ----------------------------------------------------------------------
def test_audio_player_button_disabled_until_single_row_selected(
    qt_app: QApplication, populated_db: tuple[FasSweepDb, str, list[Path]]
) -> None:
    db, run_id, _ = populated_db
    view = FasResultsView(db)
    view.load_run(run_id)
    assert view.play_btn.isEnabled() is False

    # Select row 0 explicitly.
    view.table.selectRow(0)
    qt_app.processEvents()
    assert view.play_btn.isEnabled() is True

    # Multi-select disables play (it's a single-row action).
    view.table.selectAll()
    qt_app.processEvents()
    assert view.play_btn.isEnabled() is False


# ----------------------------------------------------------------------
# CSV export
# ----------------------------------------------------------------------
def test_export_csv_writes_header_and_rows(
    qt_app: QApplication,
    populated_db: tuple[FasSweepDb, str, list[Path]],
    tmp_path: Path,
) -> None:
    db, run_id, _ = populated_db
    view = FasResultsView(db)
    view.load_run(run_id)
    dest = tmp_path / "out" / "sweep.csv"
    n = view.export_csv(dest)
    assert n == 7
    assert dest.exists()

    with dest.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    assert rows[0] == list(CSV_HEADER)
    # 7 data rows + 1 header.
    assert len(rows) == 8
    # Supplier column is at index 1.
    assert rows[1][1] == "S001"
    # Verdict at index 6 should be the raw enum string.
    assert rows[1][6] == "CONFIRMED_FAS"


def test_export_csv_uses_explicit_rows_argument(
    qt_app: QApplication,
    populated_db: tuple[FasSweepDb, str, list[Path]],
    tmp_path: Path,
) -> None:
    db, run_id, _ = populated_db
    view = FasResultsView(db)
    view.load_run(run_id)
    rows = view.selected_rows()
    # Pass a sliced subset.
    subset = rows[:3]
    dest = tmp_path / "subset.csv"
    n = view.export_csv(dest, subset)
    assert n == 3
    with dest.open(newline="", encoding="utf-8") as f:
        data = list(csv.reader(f))
    assert len(data) == 4  # 3 + header


# ----------------------------------------------------------------------
# ZIP export
# ----------------------------------------------------------------------
def test_export_zip_bundles_csv_and_wavs(
    qt_app: QApplication,
    populated_db: tuple[FasSweepDb, str, list[Path]],
    tmp_path: Path,
) -> None:
    db, run_id, wavs = populated_db
    view = FasResultsView(db)
    view.load_run(run_id)
    dest = tmp_path / "evidence.zip"
    wavs_added = view.export_zip(dest)
    assert dest.exists()
    assert wavs_added == len(wavs)

    with zipfile.ZipFile(dest) as zf:
        names = sorted(zf.namelist())
        assert "results.csv" in names
        # All wavs land under audio/ with the call_id prefix.
        wav_entries = [n for n in names if n.startswith("audio/")]
        assert len(wav_entries) == len(wavs)

        # CSV inside the zip should contain the header + every row.
        csv_bytes = zf.read("results.csv")
        reader = csv.reader(io.StringIO(csv_bytes.decode("utf-8")))
        rows = list(reader)
        assert rows[0] == list(CSV_HEADER)
        assert len(rows) == 1 + len(wavs)


def test_export_zip_skips_missing_wavs(
    qt_app: QApplication,
    populated_db: tuple[FasSweepDb, str, list[Path]],
    tmp_path: Path,
) -> None:
    db, run_id, wavs = populated_db
    # Delete one WAV so the export has a gap.
    wavs[0].unlink()
    view = FasResultsView(db)
    view.load_run(run_id)
    dest = tmp_path / "partial.zip"
    n = view.export_zip(dest)
    assert n == len(wavs) - 1
    with zipfile.ZipFile(dest) as zf:
        wav_entries = [n for n in zf.namelist() if n.startswith("audio/")]
        assert len(wav_entries) == len(wavs) - 1
        # CSV still has every row even though one WAV is gone.
        csv_bytes = zf.read("results.csv")
        rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
        assert len(rows) == 1 + len(wavs)


# ----------------------------------------------------------------------
# Detail panel
# ----------------------------------------------------------------------
def test_detail_panel_renders_for_selected_row(
    qt_app: QApplication, populated_db: tuple[FasSweepDb, str, list[Path]]
) -> None:
    db, run_id, _ = populated_db
    view = FasResultsView(db)
    view.load_run(run_id)
    # The table is sortable and is sorted on column 0 at populate time, so
    # the row PHYSICALLY at view-index 0 is not necessarily populate-order
    # row 0. The old assertion (`"S001" in text`) encoded the pre-fix BUG:
    # detail lookup was positional into _visible_rows (populate order), so
    # selecting the S002 row at view-position 0 wrongly rendered S001's
    # detail. Post-fix the panel resolves through the stable row key, so it
    # must match the supplier actually shown in the selected view row.
    view.table.selectRow(0)
    qt_app.processEvents()
    shown_supplier = view.table.item(0, COL_SUPPLIER).text()
    text = view.detail_text.toPlainText()
    assert shown_supplier in text
    assert f"Supplier: {shown_supplier}" in text

    # And a targeted check: find the view row displaying S001 (a
    # CONFIRMED_FAS row carrying the audio_reuse reason) and confirm its
    # detail renders that supplier's reasons -- proving play/export/detail
    # now follow the sorted view, not the frozen populate order.
    s001_view_row = next(
        r for r in range(view.table.rowCount())
        if view.table.item(r, COL_SUPPLIER).text() == "S001"
        and view.table.item(r, COL_SCORE).text() == "8"
    )
    view.table.selectRow(s001_view_row)
    qt_app.processEvents()
    detail = view.detail_text.toPlainText()
    assert "S001" in detail
    assert "+15551234567" in detail
    assert "audio_reuse" in detail
