"""System tray integration.

The softphone must keep running while the main window is hidden; otherwise
incoming calls never reach the user. We use QSystemTrayIcon with a minimal
menu — Available / DND / Show / Quit — per the design system rule that
2000s-style 12-item tray menus are forbidden.

Presence is local-only here (it just gates the ringer); SIP PUBLISH for
presence will follow when contacts land.
"""
from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

log = logging.getLogger(__name__)


class Presence(str, Enum):
    AVAILABLE = "available"
    DND = "dnd"


class TrayController(QObject):
    """Owns the QSystemTrayIcon and surfaces the small set of user actions."""

    show_requested = Signal()
    quit_requested = Signal()
    presence_changed = Signal(str)   # new Presence value

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._presence = Presence.AVAILABLE
        self._tray = QSystemTrayIcon(self._icon_for(self._presence), parent)
        self._tray.setToolTip("NOC_Beam — available")
        self._tray.activated.connect(self._on_activated)
        self._build_menu()
        self._tray.show()

    @property
    def presence(self) -> Presence:
        return self._presence

    @property
    def available(self) -> bool:
        """Whether the tray icon was successfully shown by the OS."""
        return self._tray.isVisible()

    def notify(self, title: str, body: str) -> None:
        """Show a balloon notification (Windows toast / macOS / Linux libnotify)."""
        if self._tray.supportsMessages():
            self._tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 4000)

    # ------------------------------------------------------------------
    # Menu construction
    # ------------------------------------------------------------------
    def _build_menu(self) -> None:
        menu = QMenu()

        act_available = QAction("Available", menu)
        act_available.setCheckable(True)
        act_available.setChecked(self._presence == Presence.AVAILABLE)
        act_available.triggered.connect(lambda: self._set_presence(Presence.AVAILABLE))

        act_dnd = QAction("Do not disturb", menu)
        act_dnd.setCheckable(True)
        act_dnd.setChecked(self._presence == Presence.DND)
        act_dnd.triggered.connect(lambda: self._set_presence(Presence.DND))

        # Mutually exclusive — drive both checked states from one source.
        self._act_available = act_available
        self._act_dnd = act_dnd

        act_show = QAction("Show window", menu)
        act_show.triggered.connect(self.show_requested.emit)

        act_quit = QAction("Quit", menu)
        act_quit.triggered.connect(self.quit_requested.emit)

        menu.addAction(act_available)
        menu.addAction(act_dnd)
        menu.addSeparator()
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_quit)
        self._tray.setContextMenu(menu)

    def _set_presence(self, presence: Presence) -> None:
        if presence == self._presence:
            self._act_available.setChecked(self._presence == Presence.AVAILABLE)
            self._act_dnd.setChecked(self._presence == Presence.DND)
            return
        self._presence = presence
        self._act_available.setChecked(presence == Presence.AVAILABLE)
        self._act_dnd.setChecked(presence == Presence.DND)
        self._tray.setIcon(self._icon_for(presence))
        self._tray.setToolTip(f"NOC_Beam — {presence.value}")
        self.presence_changed.emit(presence.value)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Single-click on Windows is Trigger; double-click is DoubleClick.
        # Treat both as "show the window".
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_requested.emit()

    # ------------------------------------------------------------------
    # Icon — load from resources if present, otherwise a coloured square.
    # ------------------------------------------------------------------
    def _icon_for(self, presence: Presence) -> QIcon:
        # Prefer the design-system SVG mark if it ships.
        res = Path(__file__).resolve().parent / "resources" / "logo-mark.svg"
        if res.exists():
            icon = QIcon(str(res))
            if not icon.isNull():
                return icon
        # Fallback: tint a flat square so presence is visible in the tray.
        pm = QPixmap(16, 16)
        pm.fill(self._presence_color(presence))
        return QIcon(pm)

    @staticmethod
    def _presence_color(presence: Presence):  # type: ignore[no-untyped-def]
        from PySide6.QtGui import QColor

        # Beam Cyan available, Danger DND.
        return QColor("#7FD3FF") if presence == Presence.AVAILABLE else QColor("#FF5C7A")
