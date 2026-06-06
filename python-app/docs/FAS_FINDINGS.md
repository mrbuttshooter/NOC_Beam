# FAS evaluation-substrate findings (2026-06)

Findings from standing up the FAS measurement substrate (replay harness +
metrics + corpus tooling) and running it against the real captured clips on
the dev machine. These answer two of the swarm review's open questions and
record what the engine actually does on real audio.

## 1. Wire rate: the models see NARROWBAND audio (open question resolved)

FFT of the largest real `fas_clips/*.wav` (stored at a 16 kHz WAV header):

| clip | rms | energy > 3.4 kHz |
|------|-----|------------------|
| call-0-1779016281 | -73 dB | 0.0% |
| call-0-1779086726 | -43 dB | 0.0% |
| call-1-1779016540 | -73 dB | 0.0% |
| call-2-1779018691 | -73 dB | 0.0% |

**Verdict: the audio reaching AASIST/Silero is 8 kHz telephone audio (G.711)
upsampled to a 16 kHz container** — there is essentially zero energy above the
~3.4 kHz G.711 cutoff. The bridge presents 16 kHz but the *content* is
band-limited.

Implication: AASIST and Silero are run on out-of-distribution input (their
spoof/speech cues above 3.4 kHz are simply absent). The swarm's rank-11
**narrowband-aware AASIST** work is therefore **justified, not a no-op** — but
it must be calibrated against a corpus before changing any weight, and should
default to a no-op offset until then.

## 2. No labelled corpus exists yet

`fas_sweep.db` is empty (`runs=0, calls=0`) — the bootstrap-from-DB path the
swarm assumed has nothing to draw on. There are ~61 loose recorded clips in
`fas_clips/`, now exported as **UNLABELED scaffold** entries
(`build/export_corpus_from_sweepdb.py`). To get ground truth: run a Test
Runner sweep (populates the DB with verdicts), and/or hand-label the scaffold.

## 3. Replay readout on the real clips (post correctness-fixes)

Running every clip through `fas_replay` (the exact live path):

- Long real-call clips (15–60 s), which the FFT shows are mostly dead air
  (−73 dB), land at **SUSPICIOUS / 30%** via the sustained-silence signal.
- Short clips → INCONCLUSIVE; sub-2 s clips → ANALYZING (warm-up gate).
- **Nothing false-fires to PROBABLE_FAS / CONFIRMED_FAS**, and AASIST stays
  correctly silent on the dead-air clips (silence-gate working).

This is consistent and sane for a clip set dominated by silent test calls. It
is also the first time these verdicts are reproducible offline.

## What this unlocks

The substrate (replay harness + pure-numpy metrics + corpus gate) is the
precondition for the swarm's *accuracy* bets — **calibrated soft-fusion** and
the **narrowband discount** — which can now be developed and validated against
ground truth instead of guessed. Next step is growing a labelled corpus
(sweep or hand-label) and freezing a `baseline.json`.
