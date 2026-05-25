# NOC_Beam — Logic Review (2026-05-25)

> Handoff note for a future session. This is a **behavioral-logic** review (does the
> app do the right thing), **not** a code-health/style review. Line numbers were
> accurate on branch `claude/vibrant-carson-43QzG` at the time of writing — re-grep
> if the files have moved. NOC_Beam is a PJSIP/PySide6 SIP softphone used for
> **supplier route testing + FAS (False Answer Supervision) detection**, primarily
> on Teles and Genband trunks.

## Read this first: missing-from-repo files

Several files the app **requires at runtime** exist only on the developer's local
machine and were never committed. A fresh clone will not build or run correctly,
and line references below may point at files you can't see in the repo:

- `src/noc_beam/ui/supplier_dropdown.py` — imported by `phone_shell.py:406` and
  `ui/test_runner_view.py:60` (hard import). Not gitignored, just never committed.
- `src/noc_beam/_native/` — the whole tree: custom `pjsua2`, `chromaprint/fpcalc`.
  Referenced by `fas_fingerprint.py:28`, `fas_smoke.py`, `sip/_pjsua2_loader.py`.
- `build/fetch_fas_models.py` + `build/MODELS.lock` — `build_windows.ps1:406` calls
  the fetch script to pull the ONNX models; both are absent. CI builds will fail
  at the test step and/or ship without models.

The shipped v1.0 `.exe` was built from a dev machine that had all of these as
untracked local files. **Action:** commit them so builds are reproducible. Until
then, the CI workflow `.github/workflows/build-windows.yml` cannot produce a
working zip (the `build` job has `needs: test`, and tests import the missing
modules).

## Already fixed this session (do NOT redo)

Commit `e4603e1` on `claude/vibrant-carson-43QzG` fixed the **end-call / "call keeps
ringing the far end" bug**:

- Root cause: `hangup_call` (`sip/endpoint.py`) set `prm.statusCode = 487` on an
  outbound early dialog. On PJSIP 2.14.1 that makes `pjsip_inv_end_session` take
  the UAS-response branch — it tears down local media but never puts a CANCEL on
  the wire. Confirmed via SIP trace (180 Ringing, then no CANCEL) + app log (no
  hangup exception; `duplicate call_id` on every dial).
- Fix: for `CALLING/EARLY/CONNECTING` leave `statusCode = 0` so the stack emits a
  clean CANCEL; explicit codes only for incoming rejects (603) and the UAS busy
  default (486). Because all teardown funnels through `hangup_call`, this also
  fixed End-all-calls and the **test runner** teardown/timeout paths.
- Also in that commit: `_on_call_requested` now enriches the reentrant
  `onCallState` record in place instead of re-registering (kills the
  `duplicate call_id` warning + lost supplier/dialed CDR metadata);
  `_hangup_one` logs find_call=None instead of silently faking teardown;
  `_on_call_record_removed` hides the card if it's still showing the removed call.

## Findings, ranked by impact

### Systemic (undercut everything else)

**S1 — FAS may be silently degraded in production.**
Every ONNX model is None-tolerant (`fas_models.py:63-83`). If the models aren't
bundled (see missing-files above), the pipeline runs on DSP features + fingerprint
only — no Silero VAD, AASIST, or PANNs — with no error. This likely explains a
high INCONCLUSIVE rate. **Verify the prod log shows `SileroVad/AasistDetector/
PannsClassifier loaded from …`** before tuning anything in `fas_rules.py`.

**S2 — The test runner cannot do per-supplier Teles routing (only Genband).**
`runner.py:_apply_routing_to_target` (runner.py:77-99) applies a supplier prefix
only when `switch_type == "genband"`. Teles routing works by swapping the account
SIP **username** to `U{id}`, and that logic lives entirely in the UI
(`phone_shell._on_supplier_changed` / `_ensure_teles_supplier_identity`,
phone_shell.py:1120-1228) — the runner never calls it. So a batch/sweep run on a
Teles account sends every call with the same username, not the supplier matrix.
**This blocks the planned FAS-sweep mode for Teles** (the production trunk).

### FAS logic

**F1 — FAS verdict can be lost from or stale on the CDR.**
`_maybe_write_cdr` fallback builder (phone_shell.py:1760-1774) omits the
`fas_verdict/confidence/reasons` fields that the `call_updated(DISCONNECTED)`
snapshot path includes (phone_shell.py:1853-1865). If `call_ended` fires before
the DISCONNECTED snapshot is stashed, the CDR has **no** FAS verdict. Even when
present, it's the last value set before disconnect; the worker scores on a
schedule (4s/8s/13s/+10s, `fas_worker.py:28`) and emits async, so short calls
record an early/`ANALYZING` reading. `TestResult.fas_verdict` (runner.py:31)
inherits the same staleness.

**F2 — Fingerprint reuse (+3, strongest signal) is structurally dark on call 1.**
It needs a prior clip for the same supplier (`fas_fingerprint.py:229-234`). A
single-shot test can never trigger it. This is the core reason a repeat/sweep
mode raises detection — it unlocks a detector, not just confidence.

**F3 — Analysis window starves PANNs.**
The worker hands every model the same 4s snapshot (`fas_worker.py:185`), but
PANNs CNN14 was trained on ~10s and the code itself notes short clips give noisy
scores (`fas_models.py:199-202`). Consider decoupling: 4s for AASIST/VAD, ~10s
rolling for PANNs + fingerprint.

**F4 — Fingerprint match threshold may be too strict for transcoded audio.**
`fingerprint_threshold = 0.90` (`fas_rules.py:80`). Audio crosses G.711 + carrier
hops; the same canned clip can return at ~0.82–0.88 and miss. Matching is already
scoped to the same `(account, supplier)`, so ~0.84 is low-risk.

### Call lifecycle

**C1 — onCallState fires reentrantly inside make_call (root cause not eliminated).**
The `duplicate call_id` warnings come from `onCallState` running synchronously on
the main thread inside `make_call`, before `_on_call_requested` finishes. The
e4603e1 fix works around it in `_on_call_requested`; the reentrancy itself remains
and could surprise other code that runs in that window.

**C2 — A second incoming call steals selection + audio focus from an active call.**
`_on_call_incoming` calls `_select_call` unconditionally (phone_shell.py:1563),
which re-routes audio focus off the live conversation. Relevant for concurrent
calls / multi-call test scenarios.

### Routing / supplier identity

**R1 — Teles routing mutates persisted account identity and re-registers per swap.**
Each supplier change rewrites `acc.username`/`acc.auth_user` and calls
`update_account` (= remove+re-add the PJSIP account; phone_shell.py:1148-1166).
Heavyweight for a many-supplier workflow, the on-disk username drifts from its
`"U"` template to the last-used `"U080"`, and two paths mutate the same fields
(deferred combo signal + pre-call materialization). It works but is brittle, and
it's the mechanism a sweep mode must drive programmatically (see S2).

### Registration

**Reg1 — IP-auth trunks (405 on REGISTER) are shown as failed.**
Retry logic is correct (405 in `_NO_RETRY_CODES`, `registration_retry.py`). But a
405 falls through to the "danger / Problem at server" status branch
(phone_shell.py:1543-1547), so an account that's perfectly usable for INVITE
(no-password IP auth — the production case) displays as failed. Cosmetic, but
misleads operators into thinking a working account is broken.

### Lower-priority fragility

- `_on_answer`/`_on_reject` (phone_shell.py:2125, 2148) ignore their `call_id`
  arg and act on the selected call. Harmless today (incoming auto-selects;
  answer/reject only on the selected widget) but breaks if added to strip rows.
- `runner._hangup` (runner.py:683) swallows all exceptions silently — add a log
  line for batch-run diagnosability.

## What's solid (don't worry about these)

- Registration backoff (1→2→4→8→16→30s, reset on 2xx) and the no-retry set.
- Call-state machine guards illegal transitions instead of crashing
  (`sip/call_manager.py`).
- Teardown is now centralized through one correct `hangup_call`.
- Audio-focus "soft-hold" design (route only the selected call; no HOLD
  re-INVITE) is sensible for multi-call testing.
- Tone/Goertzel thresholds are calibrated for post-codec PSTN levels, not
  synthetic sines (`fas_features.py`, `fas_rules.py:197-216`).

## Suggested priority order

1. **Confirm FAS models load in prod (S1)** — decides whether the detection
   problem is data or pipeline.
2. **Fix Teles routing in the runner (S2)** — prerequisite for any real FAS-sweep
   mode on the production trunk.
3. **Make the CDR/TestResult always carry the final FAS verdict (F1)** — so sweep
   output is trustworthy.
4. **Build the repeat/sweep mode (F2)** + accuracy tuning (F3/F4).
5. Commit the missing repo files so builds are reproducible (top of doc).

Items C1, C2, R1, Reg1 and the fragility list are real but lower-stakes.
