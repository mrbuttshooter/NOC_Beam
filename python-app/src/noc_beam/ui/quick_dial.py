"""Recent Calls strip below the keypad on the Dial tab.

Matches the design mockup: direction arrow + peer + SIP-status chip
+ time + green call button. Sources straight from call history (last
N CDR entries). One tap dials. A "View all" link jumps the user to
the full History tab.

Public API kept identical to the previous tile-grid version
(`call_requested`, `reload`, `MAX_TILES`, `MAX_ROWS`) so the existing
PhoneShell wiring doesn't change.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.history import CdrEntry, load_history


# --- helpers ---------------------------------------------------------

def _short_uri(uri: str) -> str:
    """Strip sip:/sips: scheme, ;params, AND @domain for compact list
    display. Compact rows in History + Recent Calls + Multi-call
    strip want just the user-part since the active account's domain
    is implicit (you dialled '200' -> 'sip:200@your-pbx' so '200'
    is what you typed). Full URI is preserved separately as a
    tooltip on each row for the curious."""
    if not uri:
        return ""
    s = uri.strip()
    if s.startswith("sip:"):
        s = s[4:]
    elif s.startswith("sips:"):
        s = s[5:]
    s = s.split(";", 1)[0]
    # Drop the @domain. If a SIP URI has no userpart (rare; e.g.
    # `sip:gateway.example.com`) keep the host so the row isn't
    # empty.
    if "@" in s:
        user, _, host = s.partition("@")
        if user:
            return user
        return host
    return s


def _display_uri(entry: CdrEntry) -> str:
    return (getattr(entry, "dialed_uri", "") or "").strip() or (entry.peer_uri or "")


def _arrow(entry: CdrEntry) -> tuple[str, str, str]:
    """Return (icon_name, hex_color, level) for the row direction indicator.

    iOS-style convention: direction arrow stays (in vs out is preserved
    visually even on failed/missed); color carries the success signal —
    green for answered, red for missed/failed. Missed-incoming gets the
    dedicated `call-missed` icon (phone + X) because it's the most
    semantically loaded state. `level` is kept for QSS hooks on the row
    (e.g. row-bg tints by `level` still work).
    """
    OK = "#2EBD5C"
    BAD = "#D33841"
    if entry.direction == "in":
        if entry.was_answered:
            return ("call-incoming", OK, "ok-in")
        return ("call-missed", BAD, "miss-in")
    if entry.was_answered:
        return ("call-outgoing", OK, "ok-out")
    return ("call-outgoing", BAD, "fail-out")


def _chip(entry: CdrEntry) -> tuple[str, str]:
    """Return (chip text, level) for the SIP-status pill.

    Operator preference: pill reads as the human label ('Busy',
    'Cancelled', 'Declined') instead of the raw code ('486'). The
    raw code stays in the row's tooltip / detail view.
    """
    from noc_beam.ui.components import sip_label
    code = entry.end_code or 0
    reason = entry.end_reason or ""
    label = sip_label(code) if code else (reason or "—")
    if 200 <= code < 300:
        level = "ok"
    elif 100 <= code < 200:
        level = "progress"
    elif code in (401, 407):
        level = "auth"
    elif 300 <= code < 400:
        level = "warn"
    elif 400 <= code < 600:
        level = "error"
    else:
        level = "muted"
    return label, level


def _fmt_time(ts: float) -> str:
    if ts <= 0:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(ts))


# --- data --------------------------------------------------------------

@dataclass(frozen=True)
class _DialTarget:
    label: str          # peer URI for display
    uri: str            # what to dial
    icon_name: str      # rail_icons key (call-outgoing / call-incoming / call-missed)
    icon_color: str     # hex stroke colour for the SVG
    arrow_level: str    # QSS hook for row-bg tinting (ok-in / fail-out / ...)
    chip_text: str
    chip_level: str
    time_text: str


# --- widgets -----------------------------------------------------------

class RecentsRow(QFrame):
    """One calm recents row: arrow / peer / chip / time / ghost redial icon.

    The WHOLE ROW is the redial affordance (double-click OR Enter), matching
    History (agent-calls' history_view.py). The per-row control is a quiet
    ghost phone icon shown only on hover -- the old filled green pill button
    is gone (brief rule: delete the repeated filled call circles).
    """

    activated = Signal(str)

    def __init__(self, target: _DialTarget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._target = target
        self.setObjectName("RecentsRow")
        self.setProperty("level", target.arrow_level)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Keyboard-focusable so Enter can redial the row (whole-row affordance).
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(38)
        # See DenseListRow / HistoryRow notes: QFrame's QSS background
        # only paints the full widget rect when WA_StyledBackground is
        # set; otherwise the hover bg only paints an inner rectangle,
        # leaving a visible "white inside grey" inset around the
        # children. Layout contentsMargins are also dropped to 0 so
        # the bg covers the full row; visual padding now lives in the
        # QSS `padding` rule on QFrame#RecentsRow.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # Direction arrow (left-most, SVG icon, coloured at render time)
        from noc_beam.ui.rail_icons import rail_icon as _rail_icon
        arrow = QLabel(self)
        arrow.setObjectName("RecentsArrow")
        arrow.setProperty("level", target.arrow_level)
        arrow.setFixedSize(18, 18)
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow.setPixmap(
            _rail_icon(target.icon_name, color=target.icon_color, px=16).pixmap(16, 16)
        )

        # Peer label (the dial target as displayed)
        peer = QLabel(target.label, self)
        peer.setObjectName("RecentsPeer")
        peer.setToolTip(target.uri)

        # Status chip (200 OK / 180 Ringing / 480 Unavailable / etc)
        chip = QLabel(target.chip_text, self)
        chip.setObjectName("RecentsChip")
        chip.setProperty("level", target.chip_level)
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Timestamp
        ts = QLabel(target.time_text, self)
        ts.setObjectName("RecentsTime")

        # Ghost phone icon (right-most): a quiet, explicit click target shown
        # only on hover. Replaces the old filled green pill; the row itself is
        # the primary redial affordance.
        self._call_btn = QToolButton(self)
        self._call_btn.setObjectName("RecentsRowCall")
        self._call_btn.setIcon(_rail_icon("calls", color="#6A6A75", px=16))
        self._call_btn.setIconSize(QSize(16, 16))
        self._call_btn.setFixedSize(QSize(28, 28))
        self._call_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._call_btn.setToolTip(f"Call {target.uri}")
        self._call_btn.clicked.connect(lambda: self.activated.emit(self._target.uri))
        self._call_btn.setVisible(False)  # hover reveals it

        row = QHBoxLayout(self)
        # contentsMargins=0 so QSS hover bg covers the full row;
        # visual padding moved into QSS on QFrame#RecentsRow.
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(arrow, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(peer, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(ts, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._call_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def enterEvent(self, event):  # noqa: N802, ANN001
        # Reveal the quiet ghost redial icon while hovering.
        self._call_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802, ANN001
        self._call_btn.setVisible(False)
        super().leaveEvent(event)

    def keyPressEvent(self, event):  # noqa: N802, ANN001
        # Enter / Return on a focused row redials it (whole-row affordance).
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.activated.emit(self._target.uri)
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802, ANN001
        # Double-click redials (Bria/History parity). Ignore when the click
        # landed on the ghost call button (its own clicked handler fires).
        if event.button() == Qt.MouseButton.LeftButton and \
                self.childAt(event.pos()) is not self._call_btn:
            self.activated.emit(self._target.uri)
        super().mouseDoubleClickEvent(event)


class QuickDialStrip(QFrame):
    """Compact recents strip. Public API kept stable for PhoneShell."""

    call_requested = Signal(str)
    view_all_requested = Signal()

    MAX_ROWS = 5
    MAX_TILES = MAX_ROWS

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("RecentsStrip")

        # Header: "Recent Calls" + "View all" link, both on one row.
        self._header = QLabel("Recent Calls")
        self._header.setObjectName("RecentsHeader")
        self._header.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self._view_all = QToolButton(self)
        self._view_all.setObjectName("RecentsViewAll")
        self._view_all.setText("View all")
        self._view_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self._view_all.clicked.connect(self.view_all_requested.emit)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(4)
        header_row.addWidget(self._header)
        header_row.addStretch(1)
        header_row.addWidget(self._view_all)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)

        self._empty_label = QLabel("No recent calls yet.")
        self._empty_label.setObjectName("RecentsEmpty")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 4)
        outer.setSpacing(2)
        outer.addLayout(header_row)
        outer.addLayout(self._rows_layout)
        outer.addWidget(self._empty_label)
        outer.addStretch(1)

        self._rows: list[RecentsRow] = []
        self.reload()

    # ------------------------------------------------------------------
    def reload(self) -> None:
        for row in self._rows:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()

        targets = self._collect_targets(self.MAX_ROWS)
        if not targets:
            self._view_all.setVisible(False)
            self._empty_label.setVisible(True)
            return

        self._view_all.setVisible(True)
        self._empty_label.setVisible(False)
        for target in targets:
            row = RecentsRow(target, self)
            row.activated.connect(self.call_requested.emit)
            self._rows_layout.addWidget(row)
            self._rows.append(row)

    # ------------------------------------------------------------------
    def _collect_targets(self, limit: int) -> list[_DialTarget]:
        try:
            history = load_history()
        except Exception:
            history = []
        history_sorted = sorted(
            history, key=lambda e: e.ended_at or 0, reverse=True
        )
        out: list[_DialTarget] = []
        # Dedupe by peer URI: Bria's recents strip shows the last N
        # DISTINCT peers, not the last N call events. Previously a
        # user who hammered redial 5 times saw five identical rows.
        seen: set[str] = set()
        for entry in history_sorted:
            uri = _display_uri(entry).strip()
            if not uri:
                continue
            if uri in seen:
                continue
            seen.add(uri)
            icon_name, icon_color, arrow_lvl = _arrow(entry)
            chip_text, chip_lvl = _chip(entry)
            out.append(_DialTarget(
                label=_short_uri(uri),
                uri=uri,
                icon_name=icon_name,
                icon_color=icon_color,
                arrow_level=arrow_lvl,
                chip_text=chip_text,
                chip_level=chip_lvl,
                time_text=_fmt_time(entry.ended_at),
            ))
            if len(out) >= limit:
                break
        return out
