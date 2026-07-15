# NOC_Beam Visual 2.0 — "go crazy" brief

The structural redesign (palette, header collapse, banners, rows, dark default)
is DONE and reviewed. The owner's verdict: still "pretty basic, pretty boring,
not modern — my team ain't using it." This pass closes the gap between
"recolored Qt app" and "modern product". Backend, signals, and business logic
stay untouched — this is chrome, typography, spacing, and motion.

## North star

The approved mockup: slate-dark instrument (#232936 canvas, #1B2130 chrome,
#2A3346 raised), one indigo accent (#5B6EE0), status colors only on statuses,
mono for numbers/codes/times. To that we now add the four things the mockup
couldn't show:

### 1. Own the window (frameless + native feel)
- Frameless main window (`Qt.FramelessWindowHint` + `WA_TranslucentBackground`
  if needed for rounded corners) with a custom title bar that is PART of the
  design: app glyph + "NOC_Beam" wordmark left, account pill + audio popover +
  hamburger integrated INTO the title bar row (one chrome row instead of
  title bar + strip), min/max/close drawn as quiet ghost buttons (hover:
  raised; close hover: danger fill).
- Preserve native behaviors via Win32: WM_NCHITTEST hit-testing for drag +
  edge resize + Aero Snap (handle in `nativeEvent`), double-click maximizes.
  Hand-roll it (~120 lines) — do NOT add GPL libraries (qfluentwidgets and
  PyQt-Frameless-Window are GPL — inspiration only, zero code copying).
- Windows 11 polish via DWM (extend `ui/native_chrome.py`):
  `DWMWA_WINDOW_CORNER_PREFERENCE = 33` → `DWMWCP_ROUND = 2` (rounded
  corners even frameless), keep the dark-titlebar attr for the AUX windows
  that stay framed. Try `DWMWA_SYSTEMBACKDROP_TYPE = 38` → Mica on the main
  window; if it fights the opaque QSS background, skip Mica — rounded +
  frameless is the priority. Everything defensive/try-except (Win10 fallback:
  square corners, framed window still works).
- Aux windows (Settings/Accounts/Trace/Test runner) KEEP native frames (they
  get dark chrome already) — only the main phone window goes frameless.

### 2. Typography that steps
- Application font: "Segoe UI Variable Text" with fallback "Segoe UI", "Inter",
  sans-serif — set once on QApplication + base QSS `font-family`. OS-provided,
  nothing bundled, no license/build risk.
- Type scale (enforce via QSS/objects, not ad hoc): window/section titles
  14px/600-weight-feel (Qt: DemiBold), body 13px, labels-muted 11px, dial
  input 20px mono, dialpad digit 20px, sub-letters 10px letterspaced,
  call-hero number 24px mono. Numbers/URIs/codes/timestamps mono
  ("Cascadia Mono", fallback "Consolas").
- Kill remaining ALL-CAPS labels except tiny 10px letterspaced kickers used
  deliberately (max one per view).

### 3. Motion (the thing Qt apps never do — 150–250ms, easing, subtle)
Build a tiny reusable helper module `ui/motion.py` (QPropertyAnimation /
QVariantAnimation wrappers, respect a global "reduce motion" kill switch
constant) and use it for:
- Bottom-tab active indicator SLIDES between tabs (animated geometry on the
  underline), icons get a quick color-fade. View switch = stacked-widget
  crossfade (opacity 0→1, 160ms OutCubic).
- Dialpad keys: pressed = scale-down to 0.96 + accent-soft flash, release
  springs back (OutBack 180ms). Hover = raise (background fade).
- Status dot on the account pill PULSES softly while "registering", solid
  when ok, still when idle.
- Inline banners slide+fade in from the top (200ms) instead of popping.
- Call hero card: entrance = fade+slide-up 220ms; the Ringing chip breathes
  (opacity 0.7↔1.0 loop) until answered, then stops.
- Buttons/pills app-wide: hover transitions via animated property (or at
  minimum QSS hover with our raised colors — but the five items above MUST
  be real animations).

### 4. Composition fixes (the mockup-fidelity gap, screen by screen)
- ONE chrome row (see §1) — then content starts with the supplier row,
  properly gridded: 12px outer gutters everywhere, 8px vertical rhythm
  between blocks, sections share left/right edges exactly.
- Supplier row: label-inside-field pattern ("Supplier" as 10px kicker above
  the combo or muted prefix inside it), chevron INSIDE the field, full-width.
- Error banner: ONE line, 32px tall, icon + elided message + Retry; operator
  wording ("Voice engine unavailable — Retry" / "Genband NY: auth failed
  (403) — Retry"), tooltip carries the technical detail.
- Dialpad: keys as proper tiles (#2A3346, radius 10px, 52–56px tall), digit
  centered 20px, letters 10px muted below, hover #323D54, press animation.
  The grid breathes: 8px gaps, equal margins.
- Recent Calls: section header 12px kicker + "View all" link right-aligned;
  rows 40px, mono number 13px, chip right, time mono muted; hover = full-row
  raise + ghost phone icon fade-in.
- Empty space below Recents must not look dead: the list stretches, and when
  short, a subtle centered muted glyph fills the void.

## Hard rules
- Palette hexes from docs/redesign/NOC_BEAM_TEST-brief.md ONLY (light values
  in light.qss; the LIGHT_TO_DARK map derives dark — new hexes need map
  entries, coordinate carefully; you own the whole visual layer this pass).
- No GPL code. No new pip deps without MIT/BSD license AND adding to
  build/noc_beam.spec consideration — prefer zero new deps.
- Keep every test green (429 pass today; `test_fas_live_demo` pre-fails).
  Contract tests pin objectNames/attributes — extend, don't break.
- PyInstaller build must keep working (`build/noc_beam.spec`); no new data
  files unless added to the spec.
- Frameless is the riskiest change: if snap/resize can't be made solid on
  this machine, fall back to framed+dark-chrome and say so — a framed window
  that works beats a frameless one that glitches.

## THE LOOP (mandatory — this is what was missing last time)
You have a live verification rig. After EVERY meaningful change batch:
1. Kill the app: PowerShell `Get-Process python | Where-Object {$_.WS -gt 50MB} | Stop-Process -Force`
   (a second launch does NOT open a window — it activates the first instance —
   so you MUST kill before relaunching).
2. Relaunch: from `python-app`: `.venv\Scripts\python.exe -m noc_beam` in background.
3. Screenshot: `.venv\Scripts\python.exe "C:\Users\User\AppData\Local\Temp\claude\E--NOC-Beam\591fef5b-7186-46f0-bda4-1af714bcd5ef\scratchpad\grab_window.py" <out.png>`
   and drive tabs/popovers with `...\scratchpad\drive_ui.py <outdir> click:X,Y shot:NAME ...`
   (coords relative to window top-left; tabs at y≈747: Dial x≈73, Contacts
   x≈168, Favorites x≈263, History x≈357).
4. LOOK at the PNG. Compare against this brief. List what's still off. Fix.
   Repeat until it matches — minimum 3 iterations, expect 5+.
Screenshot the light theme too before finishing (flip appearance.theme in
%APPDATA%/NOC_Beam/settings.json to "light", relaunch, verify, flip back to
"dark").
