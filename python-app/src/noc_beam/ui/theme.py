"""Stylesheet swap helper.

light.qss is the single source of truth for the entire app's design
language. Dark mode is derived programmatically by substituting a
fixed color map (LIGHT_TO_DARK below). Keeps the two themes from
drifting visually -- changing a colour in light.qss automatically
flows to dark via the map.

dark-hc.qss is still a hand-written high-contrast override (yellow
focus, pure black, full white text). Loaded as-is when the
high_contrast toggle is on.
"""
from __future__ import annotations

import logging
import re
from importlib import resources

from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Light -> Dark colour map.  PALETTE VERSION: NOC_BEAM_TEST v1 (indigo accent)
#
# Keys are the exact hex strings that appear in the recoloured light.qss
# (the approved "refined light" palette). Values are the approved dark
# palette. light.qss is the single source of truth; every hex it contains
# has an entry here EXCEPT #FFFFFF, which is deliberately dual-purpose:
#
#   * As a *surface* (cards / inputs / panels — the vast majority of uses)
#     it must become the dark card colour #2A3346, so #FFFFFF -> #2A3346
#     lives in this map.
#   * As *on-accent / on-status TEXT* (white label on an indigo / red
#     filled button) it must stay #FFFFFF in dark for contrast. Those few
#     selectors are re-asserted to #FFFFFF in _DARK_OVERRIDES below, which
#     is appended AFTER colour substitution so the literal white survives.
#
# Buckets: surfaces / hover / borders / text / accent (indigo) / statuses.
# All target hexes are taken verbatim from the approved dark palette:
#   window #232936, chrome #1B2130, card #2A3346, hover #323D54,
#   selected #2C3350, borders #323A4A/#3B4557, text #E3E9F2/#9AA7BD/#7C889E.
# ---------------------------------------------------------------------------
LIGHT_TO_DARK: dict[str, str] = {
    # ===== Surfaces =====
    "#FFFFFF": "#2A3346",  # card/input surface (on-accent TEXT restored below)
    "#F2F3F7": "#232936",  # window / page background
    "#E9EBF2": "#323D54",  # hover surface

    # ===== Borders =====
    "#E5E7EE": "#323A4A",  # default / subtle border, list dividers
    "#D5D9E4": "#3B4557",  # strong / input border

    # ===== Text (dark -> light) =====
    "#20232E": "#E3E9F2",  # primary
    "#6A6A75": "#9AA7BD",  # secondary
    "#9AA0B0": "#7C889E",  # muted / placeholder (also muted status fg)

    # ===== Accent (indigo — kept, only pressed lifted for dark contrast) =====
    "#5B6EE0": "#5B6EE0",  # accent base (identical in both themes)
    "#6C7EE8": "#6C7EE8",  # accent hover (identical)
    "#4A5BC9": "#6C7EE8",  # accent pressed -> lift to hover so it stays
                            #   legible as text/fill on dark surfaces
    "#EEF0FC": "#2C3350",  # accent soft / selected-row tint

    # ===== Status: ok / answered (green) =====
    "#2BA36B": "#57C98B",  # fg
    "#1D7A4F": "#7FD6A6",  # chip text
    "#E2F4EA": "#243A30",  # chip bg

    # ===== Status: danger / failed (red) =====
    "#D84B50": "#E0575B",  # fg
    "#A33A3E": "#E0888B",  # chip text
    "#F9E5E6": "#3A2C31",  # chip bg

    # ===== Status: warn / progress / ringing (amber) =====
    "#B07811": "#E8B34B",  # fg
    "#8A5F0E": "#E8C98A",  # chip text
    "#FBF1DD": "#332B23",  # chip bg

    # ===== Status: info (blue) =====
    "#3D77C2": "#6FA8E8",  # fg
    "#2B5789": "#8FC0F0",  # chip text
    "#E6F0FA": "#22344A",  # chip bg

    # ===== Status: muted / idle (reserved chip roles, future use) =====
    "#EEEFF3": "#2A3040",  # muted chip bg
    "#5F646E": "#9AA7BD",  # muted chip text
}


def _to_dark(qss: str) -> str:
    """Convert a light QSS to its dark variant via the colour map.

    Uses a single pass with a regex so each hex literal is touched at
    most once -- naive sequential replace() would re-substitute the
    output of an earlier swap.

    We match BOTH 6-digit (#RRGGBB) and 3-digit (#RGB) hex literals.
    light.qss today only uses 6-digit form, but a future PR could
    slip in `#FFF` shorthand and -- without the 3-digit branch --
    the dark substitution would silently leave it as `#FFF` (white)
    on a dark surface, producing white-on-white with no test to
    catch it. We canonicalize 3-digit to its 6-digit equivalent
    (#FFF -> #FFFFFF) and reuse the existing LIGHT_TO_DARK entries,
    rather than maintaining a parallel 3-digit table.
    """
    if not qss:
        return qss
    # 6-digit OR 3-digit hex. Order matters: try 6-digit first so we
    # don't greedily consume the first 3 chars of a 6-digit literal.
    pat = re.compile(r"#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})\b")

    def _repl(m: re.Match) -> str:
        raw = m.group(0)
        # Canonicalize 3-digit shorthand to 6-digit so LIGHT_TO_DARK
        # (which only stores 6-digit keys) can match it.
        if len(raw) == 4:  # '#' + 3 hex chars
            r, g, b = raw[1], raw[2], raw[3]
            hex_val = f"#{r}{r}{g}{g}{b}{b}".upper()
        else:
            hex_val = raw.upper()
        return LIGHT_TO_DARK.get(hex_val, raw)

    return pat.sub(_repl, qss)

REQUIRED_THEME_SELECTORS = (
    "QLabel#StatusPill",
    "QLabel#SipCodeBadge",
    "QLabel#MetricChip",
    "QToolButton#IconActionButton",
    "QFrame#FormSection",
    "QLabel#SectionHeader",
    "QFrame#FooterActionBar",
    "QPushButton#PrimaryAction",
    "QPushButton#SecondaryAction",
    "QWidget:focus",
)


def _load(name: str) -> str:
    try:
        return resources.files("noc_beam.ui.resources").joinpath(name).read_text(
            encoding="utf-8"
        )
    except Exception:
        log.warning("Could not load stylesheet %s", name, exc_info=True)
        return ""


def _substitute_assets(qss: str) -> str:
    """Replace __ASSET__ placeholders in QSS with absolute file paths
    to the bundled SVGs. Qt's QSS only accepts file URLs / paths in
    image: url(...); referencing them by name from importlib.resources
    doesn't work at runtime."""
    if not qss:
        return qss
    try:
        res_root = resources.files("noc_beam.ui.resources")
        for placeholder, asset in (
            ("__ARROW_DOWN__", "arrow-down.svg"),
            ("__ARROW_UP__", "arrow-up.svg"),
            ("__ARROW_DOWN_LIGHT__", "arrow-down-light.svg"),
            ("__ARROW_UP_LIGHT__", "arrow-up-light.svg"),
        ):
            try:
                p = str(res_root.joinpath(asset)).replace("\\", "/")
                qss = qss.replace(placeholder, p)
            except Exception:
                continue
    except Exception:
        log.warning("Asset path substitution failed", exc_info=True)
    return qss


_DARK_OVERRIDES = """
/* ===== Dark-mode-only overrides (appended after color substitution) =====
   Things that can't be expressed as a simple light->dark colour swap.

   On-accent / on-status TEXT: these buttons are filled with the indigo
   accent or the red danger colour, and their label must stay #FFFFFF for
   contrast. But #FFFFFF is mapped to the dark card surface (#2A3346) so
   filled surfaces recolour correctly -- which would also darken this
   text. Re-assert white here (this block is appended AFTER substitution,
   so the literal #FFFFFF survives). CallAvatar / QToolTip are intentionally
   NOT listed: they read better as dark-text-on-light in dark mode.
*/
QPushButton#CallButton,
QPushButton#PrimaryAction,
QPushButton#RunTestButton,
QToolButton#HistoryRowCall,
QToolButton#RecentsCallBtn,
QPushButton#EndCallButton,
QPushButton#HangupButton,
QPushButton#RejectButton {
    color: #FFFFFF;
}
"""


def load_theme_qss(*, theme: str = "light", high_contrast: bool = False) -> str:
    """Returns the QSS text for the chosen theme, or '' on failure.

    Architecture: light.qss is the single source of truth. Dark mode
    is derived programmatically via LIGHT_TO_DARK colour map -- so
    changing a colour in light.qss automatically flows to dark, and
    we can never have the two designs drift apart again.
    """
    if high_contrast:
        qss = _load("dark-hc.qss")
        return _substitute_assets(qss)

    qss = _load("light.qss")
    # Use a light-mode arrow on dark backgrounds so the chevrons stay
    # visible. Done BEFORE color substitution so the placeholder
    # token (not a hex) gets replaced.
    if theme == "dark":
        qss = qss.replace("__ARROW_DOWN__", "__ARROW_DOWN_LIGHT__")
        qss = qss.replace("__ARROW_UP__", "__ARROW_UP_LIGHT__")
        qss = _to_dark(qss)
        qss = qss + _DARK_OVERRIDES
    return _substitute_assets(qss)


def apply_theme(app: QApplication, high_contrast: bool = False, *, theme: str = "light") -> None:
    qss = load_theme_qss(theme=theme, high_contrast=high_contrast)
    log.info("apply_theme: theme=%s hc=%s qss_len=%d head=%s",
             theme, high_contrast, len(qss or ""),
             (qss or "")[:120].replace("\n", "\\n"))
    if qss:
        app.setStyleSheet(qss)
        log.info("apply_theme: QApplication.setStyleSheet applied (%d chars)", len(qss))
    # Cache the active theme on the QApplication so consumers can read it
    # without a settings.json disk read. apply_theme is the single choke
    # point for theme changes (startup + the Settings "Apply" runtime
    # switch), so this value is always fresh. The native dark-title-bar
    # filter reads it on every top-level Show event -- menus, combo popups
    # and tooltips all fire Show -- so sourcing it from disk each time added
    # a synchronous JSON read+parse to routine dropdown opens.
    try:
        app.setProperty("noc_active_theme", theme)
    except Exception:
        pass
