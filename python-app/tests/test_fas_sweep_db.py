"""Schema + round-trip tests for :mod:`noc_beam.audio.fas_sweep_db`.

Uses a tmp_path-backed sqlite file so production data is never touched.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from noc_beam.audio.fas_sweep_db import FasSweepDb


@pytest.fixture()
def db(tmp_path: Path) -> FasSweepDb:
    return FasSweepDb(tmp_path / "sweep.db")


def test_schema_is_created_on_open(db: FasSweepDb) -> None:
    """Opening the db materialises every table the spec calls out."""
    cur = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in cur.fetchall()}
    assert {"runs", "calls", "fingerprints", "cross_supplier_matches"} <= tables


def test_open_and_close_run_round_trip(db: FasSweepDb) -> None:
    run_id = db.open_run(mode="fas-sweep", tries_per_pair=3, notes="hello")
    assert run_id.startswith("sweep_")
    runs = db.list_runs()
    assert len(runs) == 1
    assert runs[0].run_id == run_id
    assert runs[0].mode == "fas-sweep"
    assert runs[0].tries_per_pair == 3
    assert runs[0].notes == "hello"
    assert runs[0].ended_at is None

    db.close_run(run_id)
    runs = db.list_runs()
    assert runs[0].ended_at is not None


def test_record_call_returns_increasing_ids(db: FasSweepDb) -> None:
    run_id = db.open_run("fas-sweep", 1)
    first = db.record_call(
        run_id=run_id,
        supplier_id="S001",
        destination_e164="+15551234567",
        try_idx=0,
        started_at=datetime.now(),
        duration_s=12.5,
        sip_final_code=200,
        fas_verdict="CONFIRMED_FAS",
        fas_score=8,
        fas_reasons=["ringback_after_200", "audio_reuse"],
        wav_path=Path("/tmp/foo.wav"),
    )
    second = db.record_call(
        run_id=run_id,
        supplier_id="S002",
        destination_e164="+15551234567",
        try_idx=0,
        started_at=datetime.now(),
        duration_s=None,
        sip_final_code=486,
        fas_verdict=None,
        fas_score=None,
        fas_reasons=[],
        wav_path=None,
    )
    assert second > first
    # Strict +1 isn't guaranteed by SQLite but in practice it is for
    # AUTOINCREMENT on a fresh table. Don't pin to it -- just monotonic.


def test_get_run_returns_calls_in_insertion_order(db: FasSweepDb) -> None:
    run_id = db.open_run("fas-sweep", 1)
    ids = []
    for i in range(5):
        cid = db.record_call(
            run_id=run_id,
            supplier_id=f"S{i:03d}",
            destination_e164="+15555550000",
            try_idx=0,
            started_at=datetime.now(),
            duration_s=1.0 + i,
            sip_final_code=200,
            fas_verdict="HUMAN_LIKELY",
            fas_score=-3,
            fas_reasons=[],
            wav_path=None,
        )
        ids.append(cid)
    rows = db.get_run(run_id)
    assert [r.call_id for r in rows] == ids
    assert [r.supplier_id for r in rows] == [f"S{i:03d}" for i in range(5)]


def test_get_call_lookup(db: FasSweepDb) -> None:
    run_id = db.open_run("fas-sweep", 1)
    cid = db.record_call(
        run_id=run_id,
        supplier_id="S007",
        destination_e164="+447700900000",
        try_idx=2,
        started_at=datetime(2026, 5, 25, 14, 0, 0),
        duration_s=4.5,
        sip_final_code=200,
        fas_verdict="SUSPICIOUS",
        fas_score=3,
        fas_reasons=["sustained_silence", "energy_stability"],
        wav_path=Path("/tmp/recordings/call.wav"),
    )
    row = db.get_call(cid)
    assert row.supplier_id == "S007"
    assert row.destination_e164 == "+447700900000"
    assert row.try_idx == 2
    assert row.fas_reasons == "sustained_silence,energy_stability"
    # str(Path(...)) is OS-specific (forward vs back slash); just make
    # sure the call survived the round trip.
    assert row.wav_path is not None
    assert "recordings" in row.wav_path
    assert row.wav_path.endswith("call.wav")
    assert row.fas_score == 3
    assert row.duration_s == pytest.approx(4.5)

    with pytest.raises(KeyError):
        db.get_call(999_999)


def test_record_fingerprint_is_byte_perfect(db: FasSweepDb) -> None:
    """The BLOB column must round-trip arbitrary bytes without mutation."""
    run_id = db.open_run("fas-sweep", 1)
    cid = db.record_call(
        run_id=run_id,
        supplier_id="S001",
        destination_e164="+1",
        try_idx=0,
        started_at=datetime.now(),
        duration_s=10.0,
        sip_final_code=200,
        fas_verdict="INCONCLUSIVE",
        fas_score=0,
        fas_reasons=[],
        wav_path=None,
    )
    payload = bytes(range(256)) + b"\x00\xff\x00\xff" * 32
    fp_id = db.record_fingerprint(cid, payload, duration_s=10.0)
    assert fp_id > 0
    fps = db.get_fingerprints_for_run(run_id)
    assert len(fps) == 1
    got_fp_id, got_call_id, got_bytes = fps[0]
    assert got_fp_id == fp_id
    assert got_call_id == cid
    assert got_bytes == payload  # byte-perfect


def test_record_fingerprint_rejects_non_bytes(db: FasSweepDb) -> None:
    run_id = db.open_run("fas-sweep", 1)
    cid = db.record_call(
        run_id=run_id,
        supplier_id="S",
        destination_e164="+1",
        try_idx=0,
        started_at=datetime.now(),
        duration_s=1.0,
        sip_final_code=200,
        fas_verdict=None,
        fas_score=None,
        fas_reasons=[],
        wav_path=None,
    )
    with pytest.raises(TypeError):
        db.record_fingerprint(cid, "not-bytes", duration_s=1.0)  # type: ignore[arg-type]


def test_record_match_dedups_via_unique_constraint(db: FasSweepDb) -> None:
    run_id = db.open_run("fas-sweep", 1)
    cid = db.record_call(
        run_id=run_id, supplier_id="S", destination_e164="+1", try_idx=0,
        started_at=datetime.now(), duration_s=1.0, sip_final_code=200,
        fas_verdict=None, fas_score=None, fas_reasons=[], wav_path=None,
    )
    fp_a = db.record_fingerprint(cid, b"abcd", duration_s=1.0)
    fp_b = db.record_fingerprint(cid, b"efgh", duration_s=1.0)

    db.record_match(fp_a, fp_b, similarity=0.91)
    # Re-record the same pair (any order) -- must NOT raise and must NOT
    # duplicate the row.
    db.record_match(fp_a, fp_b, similarity=0.87)
    db.record_match(fp_b, fp_a, similarity=0.99)

    count = db._conn.execute(
        "SELECT COUNT(*) FROM cross_supplier_matches"
    ).fetchone()[0]
    assert count == 1


def test_open_run_ids_are_unique_within_same_second(db: FasSweepDb) -> None:
    """Two opens in the same second must not collide on the PRIMARY KEY."""
    a = db.open_run("fas-sweep", 1)
    b = db.open_run("fas-sweep", 1)
    assert a != b
    # The collision-resolver appends -<n> when the base id is taken.
    assert b.startswith(a) or b.startswith(a.rsplit("-", 1)[0])
