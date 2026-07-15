from __future__ import annotations

from dataclasses import dataclass


SPACING_UNIT = 4
RADIUS_SM = 4
RADIUS_MD = 8          # buttons / inputs / cards (NOC_BEAM_TEST v1)
RADIUS_PILL = 10       # chips are pills
BOTTOM_NAV_HEIGHT = 48
ICON_BUTTON_SIZE = 28
PRIMARY_BUTTON_HEIGHT = 32
COMPACT_INPUT_HEIGHT = 32


# ---------------------------------------------------------------------------
# Approved palette (NOC_BEAM_TEST v1). Named constants so Python-side code
# (delegates, badges, painted widgets) can stop hardcoding hexes and stay in
# sync with light.qss + theme.LIGHT_TO_DARK. Light values match light.qss;
# dark values match the derived dark palette. Accent + on-accent white are
# mode-stable (identical in both themes).
# ---------------------------------------------------------------------------

# -- Accent (indigo) — the ONLY interactive colour: buttons/links/active tab
ACCENT = "#5B6EE0"
ACCENT_HOVER = "#6C7EE8"
ACCENT_PRESSED = "#4A5BC9"
ACCENT_SOFT = "#EEF0FC"          # selected-row tint (light)
ON_ACCENT = "#FFFFFF"            # text on any accent/status filled control
# Dark counterparts
ACCENT_DARK = "#5B6EE0"          # same
ACCENT_HOVER_DARK = "#6C7EE8"    # same
ACCENT_SOFT_DARK = "#2C3350"     # selected-row tint (dark)

# -- Status foregrounds (chips / dots / direction icons — never buttons)
# Each tuple entry: (fg, chip_bg, chip_text)
STATUS_LIGHT = {
    "ok":     ("#2BA36B", "#E2F4EA", "#1D7A4F"),
    "danger": ("#D84B50", "#F9E5E6", "#A33A3E"),
    "warn":   ("#B07811", "#FBF1DD", "#8A5F0E"),
    "info":   ("#3D77C2", "#E6F0FA", "#2B5789"),
    "muted":  ("#9AA0B0", "#EEEFF3", "#5F646E"),
}
STATUS_DARK = {
    "ok":     ("#57C98B", "#243A30", "#7FD6A6"),
    "danger": ("#E0575B", "#3A2C31", "#E0888B"),
    "warn":   ("#E8B34B", "#332B23", "#E8C98A"),
    "info":   ("#6FA8E8", "#22344A", "#8FC0F0"),
    "muted":  ("#7C889E", "#2A3040", "#9AA7BD"),
}

# Flat per-level fg constants (convenience for painted widgets)
STATUS_OK_LIGHT = "#2BA36B";     STATUS_OK_DARK = "#57C98B"
STATUS_DANGER_LIGHT = "#D84B50"; STATUS_DANGER_DARK = "#E0575B"
STATUS_WARN_LIGHT = "#B07811";   STATUS_WARN_DARK = "#E8B34B"
STATUS_INFO_LIGHT = "#3D77C2";   STATUS_INFO_DARK = "#6FA8E8"
STATUS_MUTED_LIGHT = "#9AA0B0";  STATUS_MUTED_DARK = "#7C889E"

# -- Neutrals (surfaces / borders / text)
SURFACE_LIGHT = {
    "page": "#F2F3F7", "card": "#FFFFFF", "hover": "#E9EBF2",
    "border": "#E5E7EE", "border_strong": "#D5D9E4",
    "text": "#20232E", "text2": "#6A6A75", "muted": "#9AA0B0",
}
SURFACE_DARK = {
    "page": "#232936", "chrome": "#1B2130", "card": "#2A3346", "hover": "#323D54",
    "border": "#323A4A", "border_strong": "#3B4557",
    "text": "#E3E9F2", "text2": "#9AA7BD", "muted": "#7C889E",
}


STATUS_LEVELS = {
    "ok": "ok",
    "progress": "progress",
    "warn": "warn",
    "danger": "danger",
    "info": "info",
    "muted": "muted",
    "running": "running",
}


@dataclass(frozen=True)
class ThemeRole:
    name: str
    meaning: str


THEME_ROLES = (
    ThemeRole("brand", "NOC_Beam mark and active navigation"),
    ThemeRole("ok", "registered, pass, call, SIP 200"),
    ThemeRole("progress", "ringing, pending, SIP 180"),
    ThemeRole("danger", "fail, missed, error"),
    ThemeRole("info", "trace and metadata"),
    ThemeRole("muted", "idle, disabled, secondary text"),
)
