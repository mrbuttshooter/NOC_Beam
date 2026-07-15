# Web softphone port — the locked target

Owner-approved final direction after seven design rounds: **the compact web
softphone** in `python-app/prototype/webui/index.html` (420x760, dark canvas
#14161d, gradient indigo Call button, glass live-call card with breathing
Ringing chip, tile keypad, badge recents). Softphone layout — NOT the console,
NOT the concepts. This brief is the implementation contract for the real port.

Branch: `redesign/web-softphone` (cut from `redesign/noc-beam-test`, so ALL
behavior fixes are present: silent restore-to-front, type-to-dial, dark title
bars, no banner/badge, compact keypad, unified aux windows).
Repo: `E:\NOC_Beam\Eyebeam` · app: `python-app` · py: `.venv\Scripts\python.exe`.

## Architecture (decided — do not relitigate)

**QWebEngineView + QWebChannel inside a Qt main window.** Verified available
in the venv. Rationale: the SIP engine, call manager, and every event in this
app is Qt-signal-driven; one Qt event loop keeps it all trivially correct, and
PyInstaller's PySide6 hooks bundle QtWebEngine automatically. pywebview was
prototype-only — do NOT use it in the app (its win32/.NET loop cannot pump Qt
signals).

New module `src/noc_beam/webui/`:
- `assets/` — `index.html` (+ split css/js if you prefer) evolved from the
  prototype: same look, but all data becomes dynamic. Also copy Qt's
  `qwebchannel.js` into assets (find it via
  `PySide6.QtWebEngineCore` resources or vendor the canonical file).
- `web_shell.py` — `WebShell(QMainWindow)`: frameless (reuse the WM_NCHITTEST
  edge-resize + rounded-corner code already in phone_shell/native_chrome on
  this branch), hosting a full-window QWebEngineView. HTML title-bar drag:
  JS mousedown on the drag region calls `bridge.start_move()` →
  `self.windowHandle().startSystemMove()`. Min/close buttons in HTML call
  bridge methods.
- `bridge.py` — the QWebChannel object. UI→Python slots:
  `place_call(target)`, `hangup()`, `toggle_mute()`, `toggle_hold()`,
  `transfer(target)`, `send_dtmf(digit)`, `select_account(id)`,
  `select_supplier(id)`, `redial(number)`, `open_window(name)` (routes to the
  EXISTING Qt windows: history, contacts, favorites, settings, accounts,
  trace, test-runner — they stay Qt in phase 1), `minimize()`, `close_win()`,
  `start_move()`. Python→UI: push JSON via
  `page.runJavaScript("nb.update(...)")` for: call state (id, peer, state,
  ring/talk seconds, account/supplier labels, codec), recents (last N from the
  history store), accounts (labels + registration health for the pill +
  dropdown), suppliers (for the select).

## Phase 1 logic strategy (pragmatic, approved)

Do NOT extract/rewrite call orchestration. `PhoneShell` already implements
supplier materialization, account switching, call flows, history writes, FAS
wiring. **Instantiate PhoneShell WITHOUT showing it** and drive it as the
logic host: call its public/`_on_*` methods from the bridge, subscribe to the
same `sip_events()` signals it uses, and mirror state to the web UI. The
hidden-window approach is explicitly acceptable for phase 1; note in code
comments that phase 2 extracts a headless CallSession service. Guard: make
sure PhoneShell never flashes visible (never call show()), the tray icon
still works (tray belongs to PhoneShell — restore-from-tray must raise the
WebShell instead: repoint `_restore_from_tray` or the activation callback in
app.py to the WebShell), and closeEvent teardown still runs on quit.

`app.py`: add a settings/env gate `NOC_BEAM_WEBUI=1`... NO — the web UI IS
the app now. Replace the shown window with WebShell (PhoneShell hidden
inside it). The single-instance activation callback raises WebShell.

## UI completeness for phase 1 (the shipping bar)

- Dial: type/paste/`/`-focus, Enter dials, Call button dials via active
  account + supplier (exactly the path the old dial field used).
- Live call card: appears on outgoing/incoming with real state transitions
  (Calling → Ringing → Connected w/ talk timer, chip colors per state), mute /
  hold / transfer / end all functional, DTMF via keypad while connected.
  Incoming shows Answer/Reject.
- Keypad: dial-string entry when idle, DTMF when in-call (same rule as
  dialpad.py `_press`).
- Recents: real last-10 from history store, live-updating, whole-row redial,
  status badges mapped from the same end-code logic history_view uses,
  "View all" opens the existing Qt History window.
- Account pill: real label + health dot (reuse `_reg_state` mapping); click
  opens the account menu (a simple HTML dropdown fed with accounts; selecting
  calls `select_account`). Supplier select: HTML select fed from the same
  supplier picker model, calls `select_supplier`.
- Hamburger (add to the HTML chrome, right of account pill): HTML menu whose
  items call `open_window(...)` for Settings / Accounts / SIP trace / Test
  runner / History / Contacts / Favorites + Quit.
- Type-to-dial and `/` focus (already in prototype JS) preserved.
- Window: frameless, rounded (Win11), draggable by chrome, edge-resizable,
  minimize-to-taskbar, close-to-tray (same close behavior as PhoneShell has
  today), 384x640 default, 360 min width.

## Rules

- The prototype's look is the spec — do not redesign it. Small additions
  (hamburger, account dropdown) must match its visual language exactly.
- No new pip deps. No CDN/network fetches in the HTML — everything local.
- The HTML/JS must not use innerHTML with dynamic data (security hook +
  actual XSS hygiene): build rows with createElement/textContent.
- Keep tests green (429 pass / 1 pre-existing fas_live_demo failure). The
  contract tests pin PhoneShell internals — hidden-host mode should keep them
  passing untouched. Add `tests/test_web_bridge.py` covering bridge slot →
  PhoneShell method routing and the state-JSON serializers (headless-safe;
  QtWebEngine itself may be unavailable offscreen — test the bridge object
  and serializers, not the rendered page).
- PyInstaller: add `src/noc_beam/webui/assets/*` to datas in
  `build/noc_beam.spec` (follow how ui/resources are bundled). QtWebEngine
  bloats the dist (~+150MB) — accepted, note final zip size in your report.
- Commit in coherent `webui(port): ...` chunks + Co-Authored-By Claude Fable
  line. Never push.

## Status — phase 3 complete (2026-07-16)

The port shipped. What exists on `redesign/web-softphone`:

- **Phase 1** (`3ffed9c`..`cba7041`): WebShell (QWebEngineView+QWebChannel,
  frameless 360x560) is THE app window; PhoneShell runs hidden as the logic
  host; bridge + serializers headless-tested; tray/single-instance repointed.
- **Phase 2** (`4a8cbc3`..`a486702`): owner P0s — multi-call card stack with
  per-call controls + compact per-call DTMF pads (main pad hides at 2+ calls),
  scrollable/keyboard-navigable dropdown menus, ~31px keypad; History /
  Contacts / Favorites live as in-app web tabs (Qt windows remain as manage /
  bulk escape hatches); dist diet: en-US locale only + WebEngineQuick dropped.
- **Phase 3** (`0f1ba63`..): Qt aux windows clamped DARK beside the dark web
  shell (stylesheet + DWM title bars); connected-call E2E verified over a real
  loopback SIP call (incoming card, answer, talk timer, mute, hold, DTMF, end,
  CDRs); QA sweep incl. fresh-profile empty states, quit/tray/activation
  paths; extra QML/Quick payload trim (dist ~732 MiB); env-gated
  `NOC_BEAM_SMOKE_DIAL` release-smoke hook; frozen build verified rendering
  AND placing a real call. Test suite: 461 passing (+1 pre-existing
  fas_live_demo failure).

Known debt (accepted): CallSession extraction (retire the hidden PhoneShell
host) stays deferred to phase 4; incoming-leg CONFIRMED transition can't
complete on the loopback rig (ACK routes to the trunk's static-NAT contact) —
verified to the extent the network allows.

## THE LOOP (mandatory)

After every batch: kill big pythons
(`Get-Process python | ? {$_.WS -gt 50MB} | Stop-Process -Force`), relaunch
`.venv\Scripts\python.exe -m noc_beam`, screenshot with
`C:\Users\User\AppData\Local\Temp\claude\E--NOC-Beam\591fef5b-7186-46f0-bda4-1af714bcd5ef\scratchpad\grab_window.py <out.png>`
(exact-title match "NOC_Beam"), LOOK at the image, fix deltas vs the
prototype. Verify at minimum: idle state, live-call state (place a call to a
bogus number through the real engine — it will ring/fail through real SIP
states), recents rows, account/supplier dropdowns open, drag/resize work.
The prototype file for pixel comparison: `python-app/prototype/webui/index.html`
(run it via `prototype/webui_poc.py` side-by-side if needed).
