# NOC_Beam visual + content style

Single source of truth for UI choices in this repo. Tokens live in
`tokens.css` (CSS variables, for any web/HTML surface), and are mirrored
into the Qt stylesheet `dark.qss`. Keep the two in sync.

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

## Layout rules

- Toolbar is fixed top, non-movable.
- Status bar is fixed bottom, always visible — carries SIP registration
  and endpoint state. It's chrome, not decoration.
- Splitter is horizontal: left rail ≤ 360 px, right pane fills.
- No floating panels, no overlays, no modal traffic outside actual
  dialogs.

## Motion

- 80 ms ease-out on hover/press background swaps.
- Pulse on the registration dot while `TRYING` (1.6 s, opacity 1 ↔ 0.35).
- 1 px solid `#7FD3FF` focus outline at 2 px offset. Visible, not glowing.
- Otherwise: no bounces, no springs, no parallax.

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

- `tokens.css` — CSS variables for any HTML/preview surface
- `dark.qss`   — Qt stylesheet derived from the same tokens
- Brand colors `#7FD3FF` / `#FFB86C` originate in `sip/trace.py`'s
  direction indicators; the rest of the system is built around them.
