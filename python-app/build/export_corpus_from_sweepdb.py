"""Build a labelled FAS evaluation corpus manifest.

Two sources, combined into one PII-safe manifest (JSONL):

  1. fas_sweep.db  -- labelled calls from completed sweeps (fas_verdict set
     and the recorded WAV still on disk) become LABELLED entries.
  2. --scan-clips DIR -- every WAV in the clip directory that isn't already
     labelled becomes an UNLABELED scaffold entry, ready for an operator to
     label (edit the "label" field) so the corpus can grow even before a
     fresh labelled sweep is run.

PII discipline (matches fas_worker's redaction): the manifest stores only a
content sha256 + basename + verdict/score/duration + a one-way supplier
bucket hash. It NEVER stores the dialed number or the raw audio, so the
manifest itself is safe to commit; the WAVs stay out of git.

Usage:
    python build/export_corpus_from_sweepdb.py \
        [--db PATH] [--scan-clips DIR] [--out tests/fas_corpus/manifest.jsonl]

With no args it uses the per-user data dir for both the DB and the
fas_clips/ directory, and writes to tests/fas_corpus/manifest.jsonl.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

# Allow running straight from the repo without installing the package.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _supplier_bucket(supplier_id: str) -> str:
    """One-way bucket so suppliers can be grouped for class balance without
    revealing the id."""
    return hashlib.sha256((supplier_id or "").encode()).hexdigest()[:8]


def _default_db() -> Path:
    try:
        from noc_beam.config.paths import data_dir
        return data_dir() / "fas_sweep.db"
    except Exception:
        from tempfile import gettempdir
        return Path(gettempdir()) / "fas_sweep.db"


def _default_clips() -> Path:
    try:
        from noc_beam.config.paths import data_dir
        return data_dir() / "fas_clips"
    except Exception:
        return Path(".")


def labelled_from_db(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT supplier_id, duration_s, sip_final_code, fas_verdict, "
            "fas_score, fas_reasons, wav_path FROM calls "
            "WHERE fas_verdict IS NOT NULL AND wav_path IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        wav = r["wav_path"]
        if not wav or not os.path.exists(wav):
            continue
        out.append({
            "sha256": _sha256(Path(wav)),
            "basename": os.path.basename(wav),
            "label": r["fas_verdict"],
            "source": "sweepdb",
            "score": r["fas_score"],
            "duration_s": r["duration_s"],
            "sip_final_code": r["sip_final_code"],
            "supplier_bucket": _supplier_bucket(r["supplier_id"]),
            "reasons": r["fas_reasons"] or "",
        })
    return out


def scaffold_from_clips(clips_dir: Path, seen_sha: set[str]) -> list[dict]:
    out: list[dict] = []
    if not clips_dir.exists():
        return out
    for wav in sorted(clips_dir.glob("*.wav")):
        sha = _sha256(wav)
        if sha in seen_sha:
            continue
        seen_sha.add(sha)
        out.append({
            "sha256": sha,
            "basename": wav.name,
            "label": "UNLABELED",
            "source": "clip_scan",
            "score": None,
            "duration_s": None,
            "sip_final_code": None,
            "supplier_bucket": None,
            "reasons": "",
        })
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--scan-clips", type=Path, default=None,
                    help="directory of WAVs to add as UNLABELED scaffold entries")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parents[1] / "tests" / "fas_corpus" / "manifest.jsonl")
    ap.add_argument("--no-scan", action="store_true",
                    help="only export labelled DB rows; skip the clip scaffold")
    args = ap.parse_args(argv)

    db_path = args.db or _default_db()
    clips_dir = args.scan_clips or _default_clips()

    labelled = labelled_from_db(db_path)
    seen = {e["sha256"] for e in labelled}
    scaffold = [] if args.no_scan else scaffold_from_clips(clips_dir, seen)

    entries = labelled + scaffold
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    by_label: dict[str, int] = {}
    for e in entries:
        by_label[e["label"]] = by_label.get(e["label"], 0) + 1

    print(f"DB:         {db_path}  ({'exists' if db_path.exists() else 'MISSING'})")
    print(f"clips:      {clips_dir}")
    print(f"labelled:   {len(labelled)} from sweepdb")
    print(f"scaffold:   {len(scaffold)} UNLABELED from clip scan")
    print(f"manifest:   {args.out}  ({len(entries)} entries)")
    print("by label:")
    for k in sorted(by_label):
        print(f"   {k:<22} {by_label[k]}")
    if not labelled:
        print("\nNOTE: no labelled calls found -- run a sweep (Test Runner) so "
              "calls land in fas_sweep.db with verdicts, OR label the UNLABELED "
              "scaffold entries by hand, then re-run to grow the corpus.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
