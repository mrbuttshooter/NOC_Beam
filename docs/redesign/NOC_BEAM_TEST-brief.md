# NOC_BEAM_TEST redesign — shared team brief

You are one of five Claude agents working **together, in parallel, on the same
branch** (`redesign/noc-beam-test`) of this repo. Other agents are editing
other files at the same time you work. This brief is the single source of
truth for the design system and the division of labor. Stay strictly inside
your assigned files — another agent owns the rest.

Repo root: `E:\NOC_Beam\Eyebeam` · App: `python-app/src/noc_beam` (PySide6).
Run tests from `python-app/` with `.venv\Scripts\python.exe -m pytest tests/<relevant> -x -q`.

## Why we're doing this

The team that was meant to adopt NOC_Beam bounced off the UI. A full design
critique found: four competing accent colors and no brand; a fixed header
block eating ~40% of every tab; heavy noisy call rows; modal error ambushes;
four auxiliary windows with four different design languages; accidental
monospace usage. We are applying ONE design system everywhere and fixing the
worst layout/behavior problems. Features and business logic do NOT change.

## The design system (approved by the owner — do not deviate)

Architecture stays: `light.qss` is the source of truth, dark is derived via
`LIGHT_TO_DARK` in `ui/theme.py`. **Dark is the new default theme.**

### Light palette ("refined light")
- Window/page background: `#F2F3F7`; cards/inputs: `#FFFFFF`; hover: `#E9EBF2`
- Borders: `#E5E7EE` (default), `#D5D9E4` (strong/input)
- Text: `#20232E` primary, `#6A6A75` secondary, `#9AA0B0` muted/placeholder
- **Accent (the ONLY interactive color, owns buttons/links/active tab/selection):
  indigo `#5B6EE0`**, hover `#6C7EE8`, pressed `#4A5BC9`, on-accent `#FFFFFF`,
  soft/selected-row tint `#EEF0FC`
- Status (statuses ONLY — chips, dots, direction icons; never buttons):
  - ok/answered: fg `#2BA36B`, chip bg `#E2F4EA`, chip text `#1D7A4F`
  - danger/failed: fg `#D84B50`, chip bg `#F9E5E6`, chip text `#A33A3E`
  - warn/progress/ringing: fg `#B07811`, chip bg `#FBF1DD`, chip text `#8A5F0E`
  - info: fg `#3D77C2`, chip bg `#E6F0FA`, chip text `#2B5789`
  - muted/idle: fg `#9AA0B0`, chip bg `#EEEFF3`, chip text `#5F646E`

### Dark palette (derived via LIGHT_TO_DARK — target values)
- Window bg `#232936`; chrome (title bar / bottom nav) `#1B2130`;
  cards/raised `#2A3346`; hover `#323D54`; selected-row tint `#2C3350`
- Borders `#323A4A` (subtle), `#3B4557` (strong)
- Text: `#E3E9F2` / `#9AA7BD` / `#7C889E`
- Accent indigo `#5B6EE0` (same), hover `#6C7EE8`, on-accent `#FFFFFF`
- Status fg: ok `#57C98B`, danger `#E0575B`, warn `#E8B34B`, info `#6FA8E8`,
  muted `#7C889E`. Chip bgs: ok `#243A30`/text `#7FD6A6`;
  danger `#3A2C31`/`#E0888B`; warn `#332B23`/`#E8C98A`;
  info `#22344A`/`#8FC0F0`; muted `#2A3040`/`#9AA7BD`

### Rules (every agent)
1. **Use ONLY palette hexes above.** Every light hex already has a dark
   mapping. If you think you need a new color, you don't.
2. Green/red/amber are status colors. The Call button, primary buttons,
   links, active tab = indigo. One indigo-filled primary button per view;
   everything else is quiet (outline/ghost).
3. Monospace by rule: phone numbers, SIP URIs, SIP codes, durations,
   timestamps → mono. All labels/headings/body → sans. Min font size 11px.
4. Radius: 8px for buttons/inputs/cards; chips are pills (10px).
5. QSS edits: your section only. `light.qss` has marked anchors
   (`/* ===== SECTION: <NAME> ===== */`) — append inside YOURS. Do not
   reformat or re-order other sections. Do not edit `theme.py`'s map
   (agent-theme owns it).
6. `REQUIRED_THEME_SELECTORS` in `theme.py` must all remain present in the QSS.
7. Errors are inline banners (warn/danger chip-style strips with a Retry
   button), never `QMessageBox`, never centered hyperlink text.
8. Empty states: muted icon + one-line invitation + action button where an
   action exists ("No contacts yet" → "Add contact").
9. Run the test files relevant to your files before declaring done; fix what
   your change broke. Do not fix unrelated pre-existing failures.
10. Commit your own work when done: `git add <your files> && git commit` with
    message `redesign(<your-stream>): <summary>`. Do NOT `git add -A` (other
    agents' half-finished work may be in the tree). Never push.

## Division of labor

### agent-theme (Phase 1 — runs alone first)
Files: `ui/resources/light.qss`, `ui/theme.py`, `ui/design_tokens.py`,
`config/store.py` (theme defaults only), `ui/settings_dialog.py` (Appearance
page default label only if needed).
- Recolor light.qss to the light palette: all orange brand (#E85D04 family)
  and green-button accents become indigo roles; status chip selectors unified
  to the chip colors above.
- Rebuild LIGHT_TO_DARK so the derived dark equals the dark palette above.
- Default theme "dark" everywhere a default exists (`store.py` — note there
  are TWO theme fields, `AppearanceSettings.theme` (default "light") at line
  ~256 and a top-level `theme` (already "dark") at ~337/408 — make them
  consistent, dark default, and check which one `app.py`/`apply_theme` reads).
- Add the QSS section anchors at end of light.qss:
  `SHELL`, `CALLS`, `AUX-WINDOWS` (empty, for the other agents).
- Tests: `test_theme_contract.py` / any theme/qss tests, `test_config_store.py`.

### agent-shell (Phase 2)
Files: `ui/phone_shell.py`, `ui/contacts_view.py`, `ui/favorites_view.py`,
`ui/title_bar.py`, `ui/rail.py`, `ui/bottom_tabs.py`. QSS section: SHELL.
- Collapse the persistent header: ONE compact status strip (account pill with
  status dot + supplier dropdown + a small warning badge that appears only
  when an account is degraded). Mic/speaker sliders + RX/TX meters move out
  of the always-on header (into the audio strip during calls, or a compact
  popover from a speaker icon button).
- Dial field + Call button visible only on the Dial tab.
- Remove the duplicated app title row (title bar already says NOC_Beam).
- Error text lines + "Click here to retry" become inline banner widgets
  (rule 7). Startup `QMessageBox.critical/warning` calls in phone_shell
  ("SIP endpoint error", "Account error") become those banners — no modals.
- Contacts/Favorites empty states per rule 8.
- Tests: `test_open_modal.py`, `test_contacts_view.py`, `test_favorites_view.py`,
  shell-related tests.

### agent-calls (Phase 2)
Files: `ui/call_widget.py`, `ui/call_list_widget.py`, `ui/history_view.py`,
`ui/audio_strip.py`, `ui/cdr_detail_dialog.py`. QSS section: CALLS.
- In-call hero: when a call exists, the call card is the dominant element —
  large mono number, account/supplier line ("via Teles UK · AAA Tel — C207"),
  state chip (Ringing = warn chip + ring timer; Answered = ok chip + talk
  timer — visually distinct), labeled control buttons (Mute / Hold / Transfer
  with text under icons), red filled hang-up. Dialpad compresses/steps back
  while a call is active (still reachable for DTMF).
- Recent Calls + History rows: whole row is the redial affordance
  (double-click / enter / hover phone icon at row end) — DELETE the repeated
  filled green circle buttons. One status chip per row. Checkboxes only
  visible in an explicit bulk-select mode (History toolbar toggle).
- Tests: `test_call_widget.py`, `test_history.py`, `test_recent_calls*.py`.

### agent-aux (Phase 2)
Files: `ui/settings_dialog.py` (layout/buttons; NOT theme defaults),
`ui/accounts_view.py`, `ui/accounts_detail.py`, `ui/trace_view.py`,
`ui/test_runner_view.py`, `ui/fas_results_view.py`, `ui/diagnostics_view.py`.
QSS section: AUX-WINDOWS.
- One indigo primary per window: Settings=Apply, Accounts=Add account,
  Test runner=Run N calls, Trace=none (all quiet). Everything else
  outline/ghost. `Export CSV` becomes a quiet button.
- Unified status pills everywhere (registration state shown ONE way: dot +
  chip, same as main window).
- Test runner: zero-count summary pills render dimmed/muted; only non-zero
  get their status color.
- Registration status "Unregistered/Unknown" plain text → muted chip.
- Tests: `test_dialog_redesign.py`, `test_test_runner*.py`, `test_fas_results_view.py`.

### agent-behavior (Phase 2)
Files: `app.py`, new module `single_instance.py` (+ its test file).
- Second-launch behavior: keep the mutex, but instead of the
  "already running, check your system tray" QMessageBox, the second instance
  signals the first (QLocalSocket to a named QLocalServer the first instance
  listens on, name e.g. "NOC_Beam_Activate") and exits 0 silently. First
  instance on receiving the signal: `showNormal() + raise_() +
  activateWindow()` on the main window (works also when minimized to tray).
  Handle stale-server cleanup (`QLocalServer.removeServer` before listen).
- Keep the mutex-failure path robust (if IPC fails, exit silently — no modal).
- Tests: add `test_single_instance.py` (server/activation logic, mockable),
  run `test_app*` if present.

## Definition of done (each agent)
- Your files follow the palette + rules; no stray hexes outside the palette.
- Relevant tests pass; `python -c "from noc_beam.ui import <your modules>"`
  imports cleanly.
- Committed with `redesign(<stream>): ...` message.
- Report back: what changed, what you could not do and why, test results.
