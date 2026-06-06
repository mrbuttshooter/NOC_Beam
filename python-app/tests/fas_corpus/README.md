# FAS evaluation corpus

This directory holds the labelled corpus that powers the FAS verdict
regression gate (`tests/test_fas_corpus_regression.py`). It is the
ground-truth that lets us *prove* a verdict change helps rather than guessing.

## Files

| File | Tracked? | Purpose |
|------|----------|---------|
| `manifest.example.jsonl` | yes | schema reference (one JSON object per line) |
| `manifest.jsonl`         | no (gitignored) | the real corpus, built on a machine with recorded clips |
| `baseline.json`          | no (gitignored) | frozen metric baseline the gate compares against |
| `*.wav`                  | no (gitignored) | recorded call audio (PII) never enters git |

The manifest stores only a content `sha256`, basename, label, and a one-way
`supplier_bucket` hash — never the dialed number or the audio itself.

## How to build / grow the corpus

```powershell
# Pull labelled calls from a completed sweep AND scaffold any loose clips:
python build/export_corpus_from_sweepdb.py
```

- **Labelled entries** come from `fas_sweep.db` calls that have a verdict and
  an on-disk WAV (run a sweep in the Test Runner to populate it).
- **UNLABELED entries** are loose clips in `%APPDATA%/NOC_Beam/fas_clips/`.
  Label them by editing the `"label"` field to the correct verdict, then
  re-run the builder to merge.

## How the gate uses it

`tests/test_fas_corpus_regression.py`:
1. skips cleanly when the models or a labelled `manifest.jsonl` are absent
   (so it never blocks CI machines that lack the ONNX models);
2. otherwise replays each labelled WAV through `fas_replay` (the exact live
   path), computes accuracy + AASIST EER via `fas_metrics`, and fails if
   accuracy drops below `baseline.json` (or the floor when no baseline yet).

To freeze a new baseline after an intentional, reviewed improvement, write
the current metrics to `baseline.json`.
