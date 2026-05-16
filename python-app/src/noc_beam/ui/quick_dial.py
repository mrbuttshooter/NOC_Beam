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


def _arrow(entry: CdrEntry) -> tuple[str, str]:
    """Return (glyph, level) where level is one of:
    'ok-out' (outgoing answered), 'fail-out' (outgoing failed),
    'ok-in' (incoming answered), 'miss-in' (missed incoming).
    """
    if entry.direction == "in":
        if entry.was_answered:
            return ("↓", "ok-in")        # ↓
        return ("↓", "miss-in")          # ↓ red
    if entry.was_answered:
        return ("↑", "ok-out")           # ↑
    return ("↑", "fail-out")             # ↑ red


def _chip(entry: CdrEntry) -> tuple[str, str]:
    """Return (chip text, level) for the SIP-status pill."""
    code = entry.end_code or 0
    reason = entry.end_reason or ""
    text = f"{code} {reason}".strip() if code else (reason or "—")
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
    return text, level


def _fmt_time(ts: float) -> str:
    if ts <= 0:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(ts))


# --- data --------------------------------------------------------------

@dataclass(frozen=True)
class _DialTarget:
    label: str          # peer URI for display
    uri: str            # what to dial
    arrow: str
    arrow_level: str
    chip_text: str
    chip_level: str
    time_text: str


# --- widgets -----------------------------------------------------------

class RecentsRow(QFrame):
    """One row matching mockup panel 1: arrow / peer / chip / time / phone."""

    activated = Signal(str)

    def __init__(self, target: _DialTarget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._target = target
        self.setObjectName("RecentsRow")
        self.setProperty("level", target.arrow_level)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(38)

        # Direction arrow (left-most, ~16 px wide, coloured)
        arrow = QLabel(target.arrow, self)
        arrow.setObjectName("RecentsArrow")
        arrow.setProperty("level", target.arrow_level)
        arrow.setFixedWidth(16)
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)

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

        # Green pill call button (right-most)
        call_btn = QToolButton(self)
        call_btn.setObjectName("RecentsCallBtn")
        call_btn.setText("☎")  # ☎ telephone glyph
        call_btn.setFixedSize(QSize(28, 28))
        call_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        call_btn.setToolTip(f"Call {target.uri}")
        call_btn.clicked.connect(lambda: self.activated.emit(self._target.uri))

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 6, 4)
        row.setSpacing(8)
        row.addWidget(arrow, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(peer, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(ts, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(call_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def mousePressEvent(self, event):  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self._target.uri)
        super().mousePressEvent(event)


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
            uri = (entry.peer_uri or "").strip()
            if not uri:
                continue
            if uri in seen:
                continue
            seen.add(uri)
            arrow_glyph, arrow_lvl = _arrow(entry)
            chip_text, chip_lvl = _chip(entry)
            out.append(_DialTarget(
                label=_short_uri(uri),
                uri=uri,
                arrow=arrow_glyph,
                arrow_level=arrow_lvl,
                chip_text=chip_text,
                chip_level=chip_lvl,
                time_text=_fmt_time(entry.ended_at),
            ))
            if len(out) >= limit:
                break
        return out
