# NOC_Beam visual + content style

Single source of truth for UI choices in this repo. Tokens live in
`tokens.css` (CSS variables, for any web/HTML surface), and are mirrored
into the Qt stylesheet `dark.qss`. A sibling `dark-hc.qss` carries the
high-contrast variant (toggled from Settings → Appearance → High
contrast). Keep all three in sync.

## Colors

Four-step cool near-black ramp, two saturated brand colors, four semantic
roles. **Never use pure white, never use pure black, no gradients, no
shadows.** Elevation is signaled by background step, not by drop shadow.

| Role                | Hex       | Where it goes                              |
|---------------------|-----------|--------------------------------------------|
| `--bg-deep`         | `#0E1116` | window chrome, inputs, behind everything   |
| `--bg-base`         | `#161B22` | main content surface                       |
| `--bg-elev-1`       | `#1F252E` | panels, list rows, raised buttons          |
| `--bg-elev-2`       | `#2A323D` | hover, dialpad keys                        |
| `--border`          | `#2A3340` | default 1px line                           |
| `--border-strong`   | `#3B4654` | hover/emphasized                           |
| `--fg-1`            | `#E6EDF3` | primary text                               |
| `--fg-2`            | `#B7C0CC` | secondary text                             |
| `--fg-3`            | `#7C8696` | muted / hint                               |
| **`--beam-cyan`**   | `#7FD3FF` | RX, primary action, focus, links           |
| **`--beam-amber`**  | `#FFB86C` | TX, secondary highlight, DTMF tones        |
| `--success`         | `#66D19E` | registered, call active, Call button       |
| `--danger`          | `#FF5C7A` | hang up, reject, error                     |
| `--warning`         | `#F0C36D` | trying, retrying                           |

Saturated colors appear **only** as signal — direction (RX/TX), state
(success/danger), action affordance (Call button). Everything else is
neutral.

## Type

- **UI:** Segoe UI on Windows (Inter fallback). 13px default, 11px
  status bar, 10px labels.
- **Mono / wire content:** Cascadia Mono. Used in the trace pane, codec
  IDs, SIP URIs, dialpad entry (18px), peer label (20px).
- **Tabular numerals** on any column of numbers.

## Spacing & radii

4px grid. 6px gutters in the dialpad (matches `dialpad.py`). Corner radii
are small: 2px on most controls, 4px on dialpad keys + cards, 6px on
dialogs + the dialpad entry. Nothing pill-shaped.

## Layout rules (v2 composition)

The window is `title bar + icon rail + content + optional drawer`.
v1's `QSplitter` + bottom `QStatusBar` are gone — rail status pill +
per-view inline state replace them.

| Region        | Dim           | Notes                                                       |
|---------------|---------------|-------------------------------------------------------------|
| Title bar     | 44 px tall    | wordmark (18 px) · active-account chip · ⌘K dial · controls |
| Icon rail     | 64 px wide    | fixed; 5 destinations + status pill at the foot             |
| Content       | flex          | `QStackedWidget` driven by the rail                         |
| Trace drawer  | 360 px wide   | right side; slides in/out via `QPropertyAnimation`          |

Rail destinations (NOC scope — Contacts, Voicemail, Conference are
deliberately excluded; see `NOC_Beam/INTEGRATION.md`):

1. Calls (default)
2. Trace
3. Accounts
4. History
5. Settings
6. (planned) Diagnostics — OPTIONS probe · ICE/STUN · TLS · REGISTER
   timing · RTCP-XR

No floating panels, no overlays, no modal traffic outside actual
dialogs. The drawer is the only resizable region.

## Motion

Match `colors_and_type.css` motion tokens. Qt QSS cannot animate, so
all motion is driven from Python via `QPropertyAnimation`; treat the
durations + curves below as constants the views consume.

| Token         | Value                            | Where it goes                            |
|---------------|----------------------------------|------------------------------------------|
| `DUR_FAST`    | 80 ms                            | hover / press / focus background swap    |
| `DUR_BASE`    | 160 ms                           | tab + segment swaps, toggle knob         |
| `DUR_SLOW`    | 240 ms                           | drawer slide, toast slide-in, modal in   |
| `PULSE_LIVE`  | 1400 ms (loop, 1 ↔ 0.35)         | ● LIVE registration dot                  |
| `PULSE_RING`  | 1600 ms (loop, 2× offset)        | incoming-call ring                       |
| `EASE_OUT`    | `cubic-bezier(0.2, 0, 0, 1)`     | house curve — every reveal               |
| `EASE_IN`     | `cubic-bezier(0.4, 0, 1, 1)`     | departures only                          |

Loops honour a "reduced motion" toggle in Settings (Qt has no direct
equivalent of `prefers-reduced-motion`). Focus is a 1 px solid
`#7FD3FF` outline at 2 px offset — visible, never glowing. No bounces,
no springs, no parallax. The SIP trace itself never animates.

## Content rules — non-negotiable

The audience is NOC engineers. They want labels, not explanations.

- **Sentence case** everywhere: "Add account", "Hang up", "SIP account".
  Title Case is reserved for proper nouns only (PCMU, G.711, NOC_Beam).
- **Brand is `NOC_Beam`** — underscore preserved, two capitals. Never
  `NocBeam`, `Noc Beam`, or `noc_beam` in user-facing copy.
- **Acronyms stay upper**: SIP, TLS, SRTP, DTMF, NAT, STUN, TURN, ICE,
  RFC, URI, RX, TX, NOC.
- **No emoji.** Anywhere. Not in copy, not as icons.
- **No exclamation marks.** Errors are stated, not yelled.
- **No marketing copy.** No "Welcome", no "Awesome", no "Let's", no
  emoji-laden onboarding. Open straight into the workspace.
- **SIP vocabulary is used directly.** REGISTER, INVITE, 401, SRTP — do
  not gloss them.
- **Ellipsis is `…`** (single character), not three periods.

## Iconography

Lucide outline icons (`https://lucide.dev`), 1.5 px stroke. No emoji, no
unicode glyphs as icons except the two typographic arrows already in the
trace pane (`→` outgoing, `←` incoming).

## Token sources

- `tokens.css`   — CSS variables for any HTML/preview surface; includes
  `@media (forced-colors: active)` and an explicit `html.hc` block
- `dark.qss`     — Qt stylesheet derived from the same tokens; carries
  motion + layout constants in its header for Python to consume
- `dark-hc.qss`  — high-contrast Qt stylesheet (pure black bg, white
  fg/borders, yellow focus); swap-in target for Settings →
  Appearance → High contrast
- Brand colors `#7FD3FF` / `#FFB86C` originate in `sip/trace.py`'s
  direction indicators; the rest of the system is built around them.
