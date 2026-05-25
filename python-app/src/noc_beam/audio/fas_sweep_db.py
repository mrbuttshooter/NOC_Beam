"""SQLite persistence for FAS sweep runs.

Stores the per-call evidence (verdict, score, reasons, recorded WAV path)
and the Chromaprint fingerprints emitted by ``fas_features`` so the
Test Runner Results pane can review a finished sweep and exporters can
package the audio evidence for distribution to the offending supplier.

The database file lives under the per-user data dir
(``%APPDATA%/NOC_Beam/fas_sweep.db`` on Windows). Schema is created
lazily on first connection -- no migrations needed for v1 since the
shape is append-only.

Public surface (see specs in the autonomous-loop brief):

    FasSweepDb()
        .open_run(mode, tries_per_pair, notes="") -> run_id
        .close_run(run_id)
        .record_call(...) -> call_id
        .record_fingerprint(call_id, fingerprint, duration_s) -> fp_id
        .record_match(fp_id_a, fp_id_b, similarity)
        .list_runs() -> list[RunRow]
        .get_run(run_id) -> list[CallRow]
        .get_call(call_id) -> CallRow

All methods are safe to call from the Qt main thread (sqlite3 in stdlib
is GIL-friendly for short writes); the test-runner is the only writer.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    mode TEXT NOT NULL,
    tries_per_pair INTEGER NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS calls (
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    supplier_id TEXT NOT NULL,
    destination_e164 TEXT NOT NULL,
    try_idx INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    duration_s REAL,
    sip_final_code INTEGER,
    fas_verdict TEXT,
    fas_score INTEGER,
    fas_reasons TEXT,
    wav_path TEXT
);

CREATE TABLE IF NOT EXISTS fingerprints (
    fp_id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES calls(call_id),
    fingerprint BLOB NOT NULL,
    duration_s REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cross_supplier_matches (
    match_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fp_id_a INTEGER NOT NULL REFERENCES fingerprints(fp_id),
    fp_id_b INTEGER NOT NULL REFERENCES fingerprints(fp_id),
    similarity REAL NOT NULL,
    UNIQUE(fp_id_a, fp_id_b)
);

CREATE INDEX IF NOT EXISTS idx_calls_run_id ON calls(run_id);
CREATE INDEX IF NOT EXISTS idx_calls_verdict ON calls(fas_verdict);
CREATE INDEX IF NOT EXISTS idx_fp_call ON fingerprints(call_id);
"""


@dataclass(frozen=True)
class RunRow:
    run_id: str
    started_at: str
    ended_at: Optional[str]
    mode: str
    tries_per_pair: int
    notes: str


@dataclass(frozen=True)
class CallRow:
    call_id: int
    run_id: str
    supplier_id: str
    destination_e164: str
    try_idx: int
    started_at: str
    duration_s: Optional[float]
    sip_final_code: Optional[int]
    fas_verdict: Optional[str]
    fas_score: Optional[int]
    fas_reasons: str           # already split-friendly comma-joined tag list
    wav_path: Optional[str]


def _default_db_path() -> Path:
    """Default sqlite path. Lives in the per-user data dir so it survives
    reinstalls and is excluded from any source-control roundup.
    """
    try:
        from noc_beam.config.paths import data_dir

        return data_dir() / "fas_sweep.db"
    except Exception:
        # Fallback for environments without platformdirs (unlikely in
        # production; keeps unit tests on tmp_path working even if
        # the import chain breaks).
        from tempfile import gettempdir

        return Path(gettempdir()) / "fas_sweep.db"


class FasSweepDb:
    """Thin wrapper around a sqlite3 connection.

    One instance per process is the intended pattern; cheap enough to
    instantiate ad-hoc for tests via the ``path=`` kwarg.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path: Path = Path(path) if path is not None else _default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe here because the test runner
        # is single-threaded on the Qt main thread; tests sometimes
        # call from worker threads via QThread. Sqlite serialises
        # writes internally so this is correct.
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------
    def open_run(self, mode: str, tries_per_pair: int, notes: str = "") -> str:
        """Create a new run row, return its ``run_id``.

        ``run_id`` is generated from the current local time at second
        precision: ``sweep_2026-05-25_14:33:09``. Collisions are vanishingly
        unlikely (would require two opens in the same second), but a
        ``-<n>`` suffix is appended on the rare case they do happen so
        the PRIMARY KEY constraint never blows up the caller.
        """
        now = datetime.now()
        started_at = now.isoformat(timespec="seconds")
        base_id = f"sweep_{now.strftime('%Y-%m-%d_%H:%M:%S')}"
        run_id = base_id
        suffix = 1
        while True:
            existing = self._conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is None:
                break
            suffix += 1
            run_id = f"{base_id}-{suffix}"
        self._conn.execute(
            "INSERT INTO runs(run_id, started_at, mode, tries_per_pair, notes) "
            "VALUES(?, ?, ?, ?, ?)",
            (run_id, started_at, mode, int(tries_per_pair), notes or ""),
        )
        self._conn.commit()
        return run_id

    def close_run(self, run_id: str) -> None:
        ended_at = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            "UPDATE runs SET ended_at = ? WHERE run_id = ?",
            (ended_at, run_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Per-call evidence
    # ------------------------------------------------------------------
    def record_call(
        self,
        run_id: str,
        supplier_id: str,
        destination_e164: str,
        try_idx: int,
        started_at: datetime,
        duration_s: float | None,
        sip_final_code: int | None,
        fas_verdict: str | None,
        fas_score: int | None,
        fas_reasons: list[str],
        wav_path: Path | None,
    ) -> int:
        """Insert one CDR-like row, return its auto-increment ``call_id``."""
        reasons_text = ",".join(r.strip() for r in (fas_reasons or []) if r and r.strip())
        cur = self._conn.execute(
            "INSERT INTO calls(run_id, supplier_id, destination_e164, try_idx, "
            "started_at, duration_s, sip_final_code, fas_verdict, fas_score, "
            "fas_reasons, wav_path) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                supplier_id,
                destination_e164,
                int(try_idx),
                started_at.isoformat(timespec="seconds"),
                None if duration_s is None else float(duration_s),
                None if sip_final_code is None else int(sip_final_code),
                fas_verdict,
                None if fas_score is None else int(fas_score),
                reasons_text,
                None if wav_path is None else str(wav_path),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def record_fingerprint(
        self, call_id: int, fingerprint: bytes, duration_s: float
    ) -> int:
        if not isinstance(fingerprint, (bytes, bytearray, memoryview)):
            raise TypeError(
                "fingerprint must be bytes-like; got %s" % type(fingerprint).__name__
            )
        cur = self._conn.execute(
            "INSERT INTO fingerprints(call_id, fingerprint, duration_s) "
            "VALUES(?, ?, ?)",
            (int(call_id), sqlite3.Binary(bytes(fingerprint)), float(duration_s)),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def record_match(self, fp_id_a: int, fp_id_b: int, similarity: float) -> None:
        """Record a cross-supplier fingerprint match.

        UNIQUE(fp_id_a, fp_id_b) means a duplicate insert is silently
        ignored -- callers don't need to dedup before calling.
        """
        # Normalise ordering so (A,B) and (B,A) collapse to the same key
        # under the UNIQUE constraint.
        lo, hi = sorted((int(fp_id_a), int(fp_id_b)))
        try:
            self._conn.execute(
                "INSERT INTO cross_supplier_matches(fp_id_a, fp_id_b, similarity) "
                "VALUES(?, ?, ?)",
                (lo, hi, float(similarity)),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            # Already recorded -- this is the documented dedup behaviour.
            pass

    # ------------------------------------------------------------------
    # Read-side accessors (powering the Results view)
    # ------------------------------------------------------------------
    def list_runs(self) -> list[RunRow]:
        """All runs, newest first."""
        rows = self._conn.execute(
            "SELECT run_id, started_at, ended_at, mode, tries_per_pair, notes "
            "FROM runs ORDER BY started_at DESC"
        ).fetchall()
        return [
            RunRow(
                run_id=r["run_id"],
                started_at=r["started_at"],
                ended_at=r["ended_at"],
                mode=r["mode"],
                tries_per_pair=r["tries_per_pair"],
                notes=r["notes"] or "",
            )
            for r in rows
        ]

    def get_run(self, run_id: str) -> list[CallRow]:
        """All calls for a run, ordered by call_id (= insertion order)."""
        rows = self._conn.execute(
            "SELECT * FROM calls WHERE run_id = ? ORDER BY call_id ASC",
            (run_id,),
        ).fetchall()
        return [self._row_to_call(r) for r in rows]

    def get_call(self, call_id: int) -> CallRow:
        row = self._conn.execute(
            "SELECT * FROM calls WHERE call_id = ?",
            (int(call_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"no call with call_id={call_id}")
        return self._row_to_call(row)

    def get_fingerprints_for_run(self, run_id: str) -> list[tuple[int, int, bytes]]:
        """Return (fp_id, call_id, fingerprint_bytes) for every fingerprint
        recorded against any call in ``run_id``. Used by the LSH index
        rebuild at the start of each sweep.
        """
        rows = self._conn.execute(
            "SELECT f.fp_id, f.call_id, f.fingerprint "
            "FROM fingerprints AS f JOIN calls AS c ON c.call_id = f.call_id "
            "WHERE c.run_id = ? ORDER BY f.fp_id ASC",
            (run_id,),
        ).fetchall()
        return [(r["fp_id"], r["call_id"], bytes(r["fingerprint"])) for r in rows]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_call(r: sqlite3.Row) -> CallRow:
        return CallRow(
            call_id=r["call_id"],
            run_id=r["run_id"],
            supplier_id=r["supplier_id"],
            destination_e164=r["destination_e164"],
            try_idx=r["try_idx"],
            started_at=r["started_at"],
            duration_s=r["duration_s"],
            sip_final_code=r["sip_final_code"],
            fas_verdict=r["fas_verdict"],
            fas_score=r["fas_score"],
            fas_reasons=r["fas_reasons"] or "",
            wav_path=r["wav_path"],
        )
