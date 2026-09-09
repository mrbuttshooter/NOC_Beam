# Changelog

All notable changes to NOC_Beam are recorded here.

## v1.3.0 — 2026-09-09

The smoothness release. Every window transition was measured in the running
app (click to first paint) and the slow paths fixed; the SIP engine and
test-runner backend are untouched.

### Speed (measured before → after)
- **Settings** 650–750 ms on every open → 38 ms. The Destinations pane built
  1,400 per-row Delete buttons each time; it now uses a lightweight ✕ cell, and
  the dialog is prebuilt while the app is idle (with a staleness check so an
  Apply or account change never shows stale values).
- **Test runner** first open 190 ms → 33 ms and **SIP trace** 40 ms → 22 ms:
  both are constructed shortly after launch instead of on first click.
- **History / Contacts / Favorites pop-outs** 110 ms → 46 ms first open, 75 ms
  → 18 ms after: rows are pre-loaded and only rebuilt when the file changed.
- **In-app tabs** no longer paint twice on every switch (data is re-pushed only
  when the history/contacts file actually changed).

### Feel
- **Call cards update in place** — mute, hold, codec and SIP state changes no
  longer replay the slide-in animation; only a new call animates.
- **Lists keep their scroll position** when a history row is expanded or a
  search is refined.
- **Tab switches** get a short fade; timers and clock columns use fixed-width
  digits so nothing shifts as they tick; text in the dial and search fields is
  selectable/copyable again.
- **Native title-bar drag** — the OS move starts on the first pixel instead of
  after a round trip through the web bridge, so dragging the softphone no
  longer stutters. Double-click on the bar still maximises/restores.

### Windows
- **Every aux window gets its own taskbar button** — Settings, Accounts, SIP
  trace, Test runner, Diagnostics and the History/Contacts/Favorites pop-outs
  are top-level now, so an open window behind the softphone can always be
  found and raised from the taskbar.

### Export
- **Full history CSV** gains **Release Code** and **Release Reason** columns
  (same as the Test runner export). Normally ended answered calls export as
  `200 OK`; unanswered calls without a wire code stay blank so counts in Excel
  never include phantom zeroes.

## v1.2.0 — 2026-07-16

The interface overhaul release. The Qt widget UI was replaced with a custom
dark web interface (rendered in-window via QWebEngineView) driving the same
proven PJSIP calling engine. No change to the SIP/FAS/test-runner backend —
only how the app looks and behaves for the operator.

### New interface
- **Custom dark web UI** — frameless, draggable, compact (360×560) main window
  with a gradient accent, replacing the old Qt widget layout. Dark is the only
  theme now (the light theme was retired as a user-facing option).
- **In-app tabs** — Dial, History, Contacts, and Favorites live inside the main
  window (bottom tab bar) instead of separate pop-up windows.
- **Live-call hero card** — active calls show a prominent card with the mono
  number, "via <account> · <supplier>" context, codec, a state chip (Ringing /
  Connected / On hold) with a live timer, and Mute / Hold / Transfer / End.
- **Multi-call stack** — each concurrent call gets its own card; with two or
  more calls the main keypad hides and each card carries its own DTMF pad so
  tones always target the intended call.
- **RX/TX audio meters** — live level bars flanking the keypad (RX left, TX
  right) for the focused call; collapse to a horizontal strip in multi-call.
- **DTMF key tones** — authentic dual-tone feedback on every key press / typed
  digit (synthesized, no audio files).
- **Searchable history** in-app with tap-to-expand call detail (account,
  supplier, codec, result, duration) and one-click redial.
- **Type-to-dial** — start typing anywhere on the Dial screen; `/` focuses the
  field; Enter places the call.

### Behavior
- **Single-instance restore** — launching a second copy silently brings the
  running window to the front (no "check your system tray" dialog).
- **Inline, non-blocking status** — registration/endpoint issues no longer pop
  modal dialogs; account health shows on the account pill's status dot.
- **Native right-click menu** — right-click opens the app menu (or Cut/Copy/
  Paste in text fields); the raw browser context menu is suppressed.
- **Per-account edit** — the account dropdown has a gear to edit an account
  directly; the Accounts window is a compact one-line-per-account list.

### Auxiliary windows
- **Dark, consistent chrome** — Settings, Accounts, SIP trace, and Test runner
  all render dark with dark title bars to match the main window.
- **Recompacted layouts** — every Settings sub-page and the Test runner were
  reorganized for density (side-by-side forms, tighter spacing, trimmed
  descriptions) without removing any control.
- **Supplier dropdown search** — type to filter large supplier catalogs;
  arrow-key navigation and proper scrolling restored.

### Fixes
- Restored the native SIP engine (`pjsua2`) and audio fingerprint tool into the
  build so packaged releases place real calls (earlier a stray move had left
  builds in UI-only "stub mode").
- Fixed the blank "Full history / Contacts / Favorites" pop-out windows.
- Fixed a clipped "Delete" button on the Destinations settings page.
- Trimmed unused engine locale/QML payload from the packaged app.

### Notes
- The packaged app is larger than v1.1.0 (~500 MB zip) because the web
  rendering engine (QtWebEngine) now ships inside it.

## v1.1.0 — 2026-07-03
- Production hardening on the Qt UI.

## v1.0.0
- Initial release.
