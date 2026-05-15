"""Recents strip below the keypad on the Dial tab.

Replaces the original 3x2 avatar grid (which read as a broken speed-dial
mosaic) with a dense single-column list of 36 px rows. One tap dials.
Sources, in priority order:

  1. Starred contacts (favorites)
  2. Recent unique peers from call history (newest first)

Caps at MAX_ROWS so the strip stays inside the narrow shell.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.contacts import load_contacts
from noc_beam.config.history import load_history


# Distinct, accessible accent palette. Hash a contact's name into one
# of these so the same person always gets the same colour.
_PALETTE = [
    "#E85D04",   # brand orange
    "#2A8DC4",   # info blue
    "#2EBD5C",   # success green
    "#C97C0E",   # warm amber
    "#7B5DD3",   # violet
    "#0EA5E9",   # cyan
    "#D33841",   # danger red
    "#0F766E",   # teal
]


def _color_for(name: str) -> str:
    if not name:
        return _PALETTE[0]
    digest = hashlib.md5(name.encode("utf-8")).digest()
    return _PALETTE[digest[0] % len(_PALETTE)]


def _initial(name: str) -> str:
    parts = (name or "?").strip().split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:1].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


def _short_uri(uri: str) -> str:
    """Strip sip:/sips: scheme and ;params for compact display."""
    if not uri:
        return ""
    s = uri.strip()
    if s.startswith("sip:"):
        s = s[4:]
    elif s.startswith("sips:"):
        s = s[5:]
    return s.split(";", 1)[0]


@dataclass(frozen=True)
class _DialTarget:
    label: str
    uri: str


class RecentsRow(QFrame):
    """One dense recents row: 20 px coloured initial circle + name + URI.
    QFrame (not QToolButton) so the child layout actually sizes."""

    activated = Signal(str)

    def __init__(self, target: _DialTarget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._target = target
        self.setObjectName("QuickDialRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(target.uri)
        self.setAccessibleName(f"Call {target.label}")
        self.setAccessibleDescription(f"Recent contact: {target.uri}")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(34)

        avatar = QLabel(_initial(target.label), self)
        avatar.setObjectName("RecentsAvatar")
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setFixedSize(QSize(22, 22))
        avatar.setStyleSheet(
            f"background-color: {_color_for(target.label)};"
            "color: #FFFFFF;"
            "border-radius: 11px;"
            "font-size: 10px;"
            "font-weight: 700;"
        )

        name = QLabel(target.label, self)
        name.setObjectName("RecentsName")

        uri = QLabel(_short_uri(target.uri), self)
        uri.setObjectName("RecentsUri")
        uri.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(8)
        row.addWidget(avatar, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(name, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        row.addWidget(uri, 0, Qt.AlignmentFlag.AlignVCenter)

    # Click anywhere on the row to dial.
    def mousePressEvent(self, event):  # noqa: N802, ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self._target.uri)
        super().mousePressEvent(event)


class QuickDialStrip(QFrame):
    """Compact recents strip. Public API kept for the existing wiring
    (`call_requested`, `reload`, `MAX_TILES` alias) so phone_shell.py
    doesn't need to change."""

    call_requested = Signal(str)

    MAX_ROWS = 5
    MAX_TILES = MAX_ROWS  # back-compat alias for callers that referenced it

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("RecentsStrip")

        # Subtle "RECENTS" divider label -- not a card with a heavy
        # bordered surface, just a typographic separator above the rows.
        self._header = QLabel("RECENTS")
        self._header.setObjectName("RecentsHeader")
        self._header.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)

        self._empty_label = QLabel(
            "No recent calls yet."
        )
        self._empty_label.setObjectName("RecentsEmpty")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 4)
        outer.setSpacing(2)
        outer.addWidget(self._header)
        outer.addLayout(self._rows_layout)
        outer.addWidget(self._empty_label)
        # Pin rows to the top of the strip; the parent gives us extra
        # vertical room because of the addWidget(..., 1) stretch on
        # dialpad_page, but we don't want to spread the rows apart.
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
            self._header.setVisible(False)
            self._empty_label.setVisible(True)
            return

        self._header.setVisible(True)
        self._empty_label.setVisible(False)
        for target in targets:
            row = RecentsRow(target, self)
            row.activated.connect(self.call_requested.emit)
            self._rows_layout.addWidget(row)
            self._rows.append(row)

    # ------------------------------------------------------------------
    def _collect_targets(self, limit: int) -> list[_DialTarget]:
        seen: set[str] = set()
        out: list[_DialTarget] = []

        try:
            contacts = load_contacts()
        except Exception:
            contacts = []
        for c in contacts:
            if not c.favorite:
                continue
            uri = (c.number or "").strip()
            if not uri or uri in seen:
                continue
            seen.add(uri)
            out.append(_DialTarget(label=c.name or _short_uri(uri), uri=uri))
            if len(out) >= limit:
                return out

        try:
            history = load_history()
        except Exception:
            history = []
        history_sorted = sorted(history, key=lambda e: e.ended_at, reverse=True)
        contact_by_uri = {self._normalize(c.number): c for c in contacts}
        for entry in history_sorted:
            uri = (entry.peer_uri or "").strip()
            if not uri or uri in seen:
                continue
            seen.add(uri)
            existing = contact_by_uri.get(self._normalize(uri))
            label = existing.name if existing else _short_uri(uri)
            out.append(_DialTarget(label=label, uri=uri))
            if len(out) >= limit:
                return out

        return out

    @staticmethod
    def _normalize(uri: str) -> str:
        return _short_uri(uri).lower()
