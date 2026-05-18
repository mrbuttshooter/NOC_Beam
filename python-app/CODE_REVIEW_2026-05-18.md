# NOC_Beam — Full Pre-Rollout Code Review

**Date:** 2026-05-18
**Commit reviewed:** `e810ac8`
**Build artifact:** `dist/NOC_Beam.zip` (85 MB, v2026-05-18 release)
**Scope:** Full codebase audit across 5 subsystems by parallel reviewers.
**Total findings:** ~100 raw, consolidated to ~30 unique items below.

---

## TL;DR for the rollout decision

The app is **structurally sound and substantially more disciplined than typical pre-rollout code** — explicit lock ordering in SIP, atomic writes with corruption-quarantine in config, debounced search + diff-based strip refresh in the UI, real test coverage on the test runner state machine.

**BUT** there are 3 cross-cutting bug classes that hit multiple subsystems:

1. **Signal/subscriber lifecycle is the weakest link.** `destroyed.connect(...)` is used in 5+ places to disconnect from `sip_events()` singletons; PySide6 fires `destroyed` too late (sometimes never), so dead closures accumulate. Re-opening Settings 20× during a NOC shift = 40 dead subscribers eventually firing `setProperty` on deleted QLabels → `RuntimeError: Internal C++ object already deleted`.
2. **PJSIP worker thread races Qt main thread on shared mutable state.** `acc.calls`, `self._accounts`, `self._states` are all iterated from one thread while mutated from another, without a lock. `dict changed size during iteration` is one bad-timing call away.
3. **FAS verdict is currently unreliable.** PANNs CNN14 is fed the **oldest 1 second** of a 4-second buffer (not the most recent), and only 1s vs the 10s the model was trained on. The `music_on_hold` / `silence` / `ringing` rule-engine weights are systematically based on stale, undersized context.

**Recommendation:** Fix the 12 Day-1 blockers below (est. 4-6 hr of focused work), rebuild, then roll. The first-week and polish items can ship in the next iteration. **Do not let engineers make automated routing decisions on the FAS verdict** until Critical #11/#12 land.

---

## Day-1 blockers (must fix before colleague rollout)

### Threading & races

**#1 — SIP — Unlocked iteration of `self._accounts` / `acc.calls`** · `sip/endpoint.py:742-751, 787-839` · `sip/account.py:65-74` · `sip/call.py:80-84`
PJSIP worker thread appends to `acc.calls` in `onIncomingCall` and removes in `onCallState(DISCONNECTED)` while the Qt main thread iterates the same dict from `find_call()`, `set_call_audio_focus()`, `set_call_mute()`, `CallQualitySampler._poll`. The endpoint's `RLock` exists and is held in `start`/`stop`/`add_account`/`remove_account`, but read paths bypass it.
**Fix:** snapshot under the lock — `with self._lock: accs = list(self._accounts.values()); call_lists = {a.id: list(a.calls) for a in accs}` — then iterate the copies. The lock is re-entrant so this composes with anything else.

**#2 — SIP — `onCallMediaState` bypasses audio focus router** · `sip/call.py:88-131`
On every media-state activation, runs `capture → aud` + `aud → playback` from the PJSIP thread unconditionally. For ~200 ms after every multi-call answer, mic leaks into the unfocused trunk and the focus router is defeated until the user clicks something.
**Fix:** have `onCallMediaState` emit `call_media_active`; let a single main-thread handler in `phone_shell` call `set_call_audio_focus(currently_focused_call_id)`. Do not call `startTransmit` from the PJSIP callback.

### Signal/subscriber lifecycle

**#3 — UI views — `destroyed.connect(...)` for sip_events teardown is unreliable** · `settings_dialog.py:957,1001` · `accounts_detail.py:108-116` · `trace_view.py:711-714`
PySide6 doesn't reliably fire `destroyed` for parented `QDialog`s that get `accept()`/`reject()`-ed. The closure captures `self`, so the slot can run after the C++ side is gone, OR (more often) never runs, leaking subscribers permanently. Each Settings re-open adds 2 more dead closures.
**Fix:** standardize on **`closeEvent` override + explicit `shutdown()` method** called by the parent. Drop `destroyed.connect` everywhere. Same pattern needed in `phone_shell.py:545` strip-refresh lambdas (currently disconnected only in `closeEvent` — same fragility).

**#4 — UI views — `account_dialog.py:402` self._test_conn is permanently None** · `account_dialog.py:402,437-441`
`self._test_conn = signal.connect(...)` — `Signal.connect()` returns `None` in PySide6 (not a `Connection` object). The guard `if self._test_conn is not None:` is therefore always false, so the disconnect path is **dead code**. Every Test Registration click leaks one subscriber.
**Fix:** use `self._test_subscribed: bool = False` instead; set True on connect, gate disconnect on it, reset on `_cleanup_test`.

**#5 — SIP — `RegistrationRetry` doesn't reset timers on account remove** · `sip/endpoint.py:572-580` · `sip/registration_retry.py` · `ui/phone_shell.py:1187,1196,1217`
When `remove_account` is called (and the same `account_id` is re-added milliseconds later via the supplier-swap remove+re-add path), a stale QTimer can fire `setRegistration(True)` on the freshly-registered new account, racing the legitimate REGISTER. Also leaks timers across long sessions of churning accounts.
**Fix:** `phone_shell` callers of `remove_account` (and the supplier-swap path) need to call `reg_retry.reset(account_id)` before re-add. OR have endpoint emit `sip_events().account_removed` and let RegistrationRetry listen.

**#6 — UI shell — `_reg_state` / `_last_call_peer` never pruned** · `phone_shell.py:1296,1530-1532`
These dicts grow on account-add / call-update but are never pruned on remove. Long-running NOC sessions with supplier churn accumulate dead keys; if the same account_id is ever reused, `_set_active_account` consults stale state.
**Fix:** `self._reg_state.pop(account_id, None)` in `_remove_account_by_id`; same for `_last_call_peer` in `_on_call_record_removed`. Init both dicts in `__init__` (currently `_last_call_peer` is lazy-created via `hasattr` in the hot path).

### Data loss

**#7 — Config — `_atomic_write` PermissionError fallback is non-atomic** · `config/store.py:369-375`
After `tmp.replace(path)` fails (AV scan, file lock), falls back to `path.write_text(content)` — naked open-truncate-write. Crash mid-write = empty `accounts.json` = all SIP credentials lost on next launch.
**Fix:** retry the `tmp.replace` with backoff (the pattern `history.py:142-149` already uses), don't degrade to non-atomic write.

**#8 — Config — `history.append_entry` cache mutated before save retry** · `config/history.py:155-156,168-172`
Appends to module-level `_cache` *before* the save retry loop. If all 3 retries fail, cache and disk diverge silently for the rest of the session, and the next successful save trims from the divergent cache (losing real CDRs).
**Fix:** roll back the cache append in the failure branch, OR cache-after-save-success only.

### FAS correctness

**#9 — Audio — PANNs CNN14 fed oldest 1s of 4s buffer, 10× less than trained on** · `audio/fas_models.py:200-204`
`x[:16000]` slices the **first** 1 s of a snapshot that is by construction the **most recent** 4 s — so verdicts lag by 3 s and miss late-arriving ringback. And 1 s is 10× less context than CNN14 was trained on (~10 s clips), making `music_on_hold` / `silence` / `ringing` scores systematically noisy. Combined with the rule engine's monotonic-severity lock, an early misclassification at t=3 s pins a wrong verdict for the whole call.
**Fix:** `x = x[-target:]` (take *last* N samples) AND set target to `min(len(x), 16000*10)` to use up to 10 s. CNN14 handles variable-length input.

**#10 — Audio — ONNX session leak in `shutdown_fas_engine`** · `audio/fas_models.py:288` (`shutdown_fas_worker`)
Worker singleton is shut down but module-level `_silero / _aasist / _panns` sessions are never released. Process restart inside the same Python interpreter (test runs, hot reload) leaks them.
**Fix:** add `shutdown_models()` in `fas_models.py` that nulls the globals; call from `stop_fas_engine()` after `shutdown_fas_worker()`.

### Test-runner shared mutable state

**#11 — Testing — `_active_supplier_id` stamped on shared `AccountConfig`** · `testing/runner.py:798-801`
Runner does `setattr(acc, "_active_supplier_id", ...)` on live `AccountConfig` objects also held by `phone_shell` and serialized to `accounts.json`. No cleanup after batch ends → every account carries last-run's supplier ID into subsequent code paths. If anything ever runs `asdict()` or `json.dumps(default=...)`, the underscore field can leak to disk or be silently dropped.
**Fix:** pass `supplier_id` as an explicit `Runner` constructor param, OR build transient deep-copies of accounts for the run. Don't stamp shared state.

### UI footgun

**#12 — UI shell — Monkey-patched `mousePressEvent` on `QFrame` captures unbound method** · `phone_shell.py:2085-2092`
`_original = QFrame.mousePressEvent` captured at construction; `_press` closure references `_row`. When the row is `deleteLater()`-ed, Python holds the closure cell past Qt's deletion. If a queued mouse event fires after delete, `_original(_row, ev)` calls into a dead C++ object → RuntimeError.
**Fix:** subclass `QFrame` and override the method properly. Don't monkey-patch instance methods on Qt widgets.

---

## First-week issues (rollout but fix in next sprint)

### Security / persistence

- **DPAPI fallback to base64 is silent on Windows** (`config/store.py:36-38`). Only `log.warning` — domain roaming-profile glitches silently downgrade every account's password to b64. Surface a UI banner; don't degrade silently.
- **`accounts.json` has no ACL hardening** (`config/store.py:358-361`). DPAPI is CurrentUser scope — *any* same-user process (browser extension, downloaded EXE) can decrypt. Add `icacls` / `win32security` lock-down on first write.
- **No single-instance mutex** (`config/history.py:27-28`). Two NOC_Beam instances both append to `call_history.json`; last-writer-wins drops the other's CDRs. PJSIP already wants a singleton — enforce it at boot.
- **`contacts.py` lacks atomic-write retry + quarantine** (`config/contacts.py:33-46`). Plain `tmp.replace`, no `_filter` on `Contact(**item)` — adding a `notes` field will TypeError on every row written by an older build → empty list returned → next save wipes the file.
- **History 1000-entry cap silently drops oldest** (`config/history.py:19`). 50-200 test calls/day = ~1 week before drops. For carrier compliance, rotate to `call_history.YYYY-MM.json` instead.

### SIP

- **Password could leak via `__repr__`** (`sip/account.py:124`). `cfg.password` is held plain-text on `AccountConfig`; one stray `log.info("cfg=%r", cfg)` and credentials are in logs. Override `AccountConfig.__repr__` to redact.
- **`make_call` releases lock before `call.makeCall()`** (`sip/endpoint.py:611-633`). Necessary (DNS can block 30 s) but `remove_account` can land in the window and `acc.shutdown()`. Either refuse-or-defer `remove_account` when `acc.calls` non-empty (the UI does this; the endpoint should too defensively), or re-check membership after `makeCall` returns.
- **`_AUTH_REJECT_CODES` includes 423** (`sip/registration_retry.py:26`). 423 is Interval Too Brief, not auth rejection. Correct response is re-REGISTER with `Min-Expires`. Misnamed at minimum.
- **`stop()` holds lock across `libHandleEvents(50)`** (`sip/endpoint.py:222-265`). 1.5 s shutdown loop with the lock held stalls anything that needs `get_account` in that window. Release lock around `libHandleEvents`.

### UI shell

- **`light.qss` has 8+ duplicate selectors** (`light.qss:1762-1809, 1597 vs 1653`). `QFrame#TopStrip`, `QToolButton#TabBtn`, `QPushButton#CallButton`, etc. defined twice — later rule silently overrides the first, flipping padding/border-radius from the apparent first definition. Audit + fold; add a CI lint rule.
- **LIGHT_TO_DARK regex doesn't catch 3-digit hex** (`theme.py:117`). A single future `#FFF` slips through unchanged → white-on-white in dark mode with no test catching it. Either expand regex or assert in CI that `light.qss` has no `#XXX` shorthand.
- **`audio_strip.py:243` right-click pre-`_start_sip` crashes** (`audio_strip.py:243-248`). Menu lazy-created in `set_input_devices`; nullptr-deref window in first ~500 ms. Guard.
- **Reaching into `SipEndpoint._ep` from shell** (`phone_shell.py:1767-1788`). Layering violation, no guard for `_ep is None` during startup. Add a public `SipEndpoint.set_playback_mute(bool)`.
- **`_refresh_calls_strip` rebuilds on every `call_updated`** (`phone_shell.py:548`). Diff-based now (good) but still wakes on FAS verdicts + quality samples + codec changes — 50+ times/sec under 10-call load. Coalesce with 50 ms single-shot.

### UI views

- **`history_view` visible-list recomputed in 3 places, racing CDR appends** (`history_view.py:606,667,678`). User double-clicks row N; CDR arrives between click and `_open_detail`; user sees wrong CDR. Cache `self._visible: list[CdrEntry]` in `_refresh_rows`.
- **`accounts_detail` re-register failure leaves UI in lie state** (`phone_shell.py:1126-1130 / 1192-1195`). After `_save_accounts_or_warn` returns False, `self.accounts` still has the new/edited row — UI shows what disk doesn't. Wrap in try/restore.
- **`settings_dialog` Reset codecs does nothing** (`settings_dialog.py:1126-1146`). Reads from legacy `_codec_priority_spins` (always empty after drag-drop refactor). Repopulate the two QListWidgets instead.
- **`settings_dialog` 2 s timer outlives the button** (`settings_dialog.py:316-323`). "Saved ✓" → `singleShot(2000, reset_text)` with no parent. If user closes dialog within 2 s, slot fires on deleted button → RuntimeError. Parent the QTimer to `save_btn`.
- **`settings_dialog.py:322` `log.exception(...)` but no `log` imported.** NameError at runtime if save fails.
- **`cdr_detail_dialog` shows raw account UUID** (`cdr_detail_dialog.py:85`). Use `_resolve_account_label` (already exists in history_view). Plus export uses blocking `QMessageBox.information` instead of the toast everywhere else uses.
- **Trace export bypasses `default_export_dir`** (`trace_view.py:856`). Lands in `log_dir()` instead of `Documents/NOC_BEAM/` — inconsistent with History + Test Runner exports.

### Audio

- **`FailureTone.play_for_code` stop+play race** (`audio/ringer.py:265-281`). `fx.stop(); fx.play()` — `stop()` is async; back-to-back rejects clip the onset. Pre-create N parallel QSoundEffect per tone, OR defer via `playingChanged`.
- **Ringer single-instance ignores concurrent inbound** (`audio/ringer.py:76-117`). Second incoming call doesn't ring; first hangup cuts ringer for both. Refcount or document as out of scope.
- **`_per_call` dict has no shutdown lock** (`audio/fas_engine.py:33,63-69,90-111`). PJSIP can callback during `libDestroy()` — `attach_fas_to_call` during shutdown gets a `RuntimeError`. Add `_shutting_down` flag.
- **Silero `_state` is dead code** (`audio/fas_models.py:108-127`). Local `state = np.zeros(...)` shadows it on every `score()` — original intent (carry state across ticks) was abandoned. Delete `self._state` or actually carry it.

---

## Polish (post-rollout)

- Multiple `__import__("PySide6.QtCore", fromlist=["Qt"])` shims in test_runner_view hot paths — remnant of refactor, import once at module top.
- `subprocess.Popen(["explorer", "/select,", str(path)])` toast handler — gate on `sys.platform == "win32"`.
- Several `Qt.AlignVCenter` (deprecated short form) usages — bump to `Qt.AlignmentFlag.AlignVCenter`.
- `rail_icon` returns empty `QIcon()` silently when name not found — log WARN on miss.
- `contacts_view._on_add_group` emits signal *before* QInputDialog confirms — move emit after `if ok and group:`.
- `_emit_result` uses `sip_call_id=-1` as a dict key in `_fas_by_call_id.pop` — use `None` and gate the lookup explicitly.
- `trace_view._on_export` doesn't delete partially-written file on disk-full mid-write.
- `_resolve_account` in test runner falls through to `self.accounts[0]` even when *no* enabled accounts exist — should return None with clear "no enabled account" result.
- Inline stylesheets in `account_dialog.py:464` and `diagnostics_view.py:198,202` bypass theme tokens — convert to `setProperty("level", ...)` like everywhere else.
- `_SIP_CODE_LABEL` missing 1xx provisional codes (100/180/181/182/183).
- Several lazy-init via `hasattr` checks in hot paths — hoist to `__init__`.

---

## Per-subsystem verdict (from reviewers)

| Subsystem | Verdict |
|---|---|
| **SIP** | "Sign off only after Critical #1-#4 fixed — unlocked iteration is a real crash under multi-call load, not theoretical." |
| **Audio** | "FailureTone is clean and ships. **Don't make automated routing decisions on FAS verdict until C1/C2 land.**" |
| **UI shell** | "Substantially more disciplined than v3-era. Push every binding through SignalRegistry, prune dicts on remove, coalesce strip refresh — then rollout-ready." |
| **UI views** | Most views ready; trace_view is best in class. Settings + accounts_detail + account_dialog + test_runner_view each need one critical fix. |
| **Config + testing** | "Solid for single-instance happy path. Non-atomic fallback + multi-instance + cache divergence are ship-blockers for a team that will occasionally double-launch." |

---

## Suggested fix order

**Sprint 1 (this evening, ~4-6 hr):** Day-1 blockers #1-#12 above.

**Sprint 2 (next week):** First-week issues — security/persistence first, then SIP polish, then UI views.

**Sprint 3 (whenever):** Polish.

A focused review pass on `_signal_registry.py` to make it the **single** sip_events disconnect path (replacing every `destroyed.connect` and every hand-disconnected lambda) would close ~half the critical findings on its own.
