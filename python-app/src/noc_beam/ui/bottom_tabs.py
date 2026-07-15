"""Bottom tab bar -- Bria-parity 4-icon navigation.

Four icon buttons across the bottom of the phone shell, mapped 1:1
to Bria's tab structure: Dialpad / Contacts / Favorites / History.
Active button gets the orange accent + a 2 px top border. Clicking
emits `tab_changed(int)` so the shell can swap the content area.

NOC-specific surfaces (SIP trace, accounts management, diagnostics)
are NOT in the bottom tabs -- they live behind the View menu, opened
in their own windows. Keeps the main window visually identical to
Bria for users who only need the softphone, and gives NOC engineers
the deeper tools without crowding the primary UI.
"""
from __future__ import annotations

from enum import IntEnum

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QToolButton,
    QWidget,
)

from noc_beam.ui.design_tokens import BOTTOM_NAV_HEIGHT
from noc_beam.ui.rail_icons import rail_icon


class Tab(IntEnum):
    DIALPAD   = 0
    CONTACTS  = 1
    FAVORITES = 2
    HISTORY   = 3
    # TRACE was removed from the bottom tabs. The lower nav is
    # call-flow chrome (things the user reaches for DURING a call);
    # SIP trace is a diagnostic surface that doesn't belong in that
    # mental model. Bria/Zoiper/Linphone don't surface packet trace
    # inline either. The popup window (View -> SIP Trace, Ctrl+Shift+T)
    # is the only entry point now.


_TABS: tuple[tuple[Tab, str, str, str], ...] = (
    (Tab.DIALPAD,   "grid",  "Dial",      "Dialpad"),
    (Tab.CONTACTS,  "user",  "Contacts",  "Contacts and groups"),
    (Tab.FAVORITES, "star",  "Favorites", "Starred contacts"),
    (Tab.HISTORY,   "clock", "History",   "Call history"),
)


class BottomTabs(QFrame):
    tab_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("BottomTabs")
        self.setFixedHeight(BOTTOM_NAV_HEIGHT)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[int, QToolButton] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        for tab, icon_name, short_label, tip in _TABS:
            btn = QToolButton(self)
            btn.setObjectName("TabBtn")
            btn.setCheckable(True)
            btn.setIcon(rail_icon(icon_name, color="#9AA0B0", px=18))
            # Active-state icon coloured with the indigo accent so the
            # checked state reads as a colour change, not just a border.
            on = rail_icon(icon_name, color="#5B6EE0", px=18).pixmap(18, 18)
            ic = btn.icon()
            from PySide6.QtGui import QIcon

            ic.addPixmap(on, QIcon.Mode.Selected)
            ic.addPixmap(on, QIcon.Mode.Active)
            btn.setIcon(ic)
            btn.setIconSize(QSize(18, 18))
            btn.setText(short_label)
            btn.setToolTip(tip)
            btn.setAccessibleName(tip)
            btn.setAccessibleDescription(f"Switches to {tip}. Shortcut Ctrl+{int(tab) + 1}.")
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            btn.setAutoExclusive(True)
            self._group.addButton(btn, int(tab))
            self._buttons[int(tab)] = btn
            layout.addWidget(btn, 1)

        self._group.idClicked.connect(self._on_id_clicked)
        self._buttons[int(Tab.DIALPAD)].setChecked(True)

        # Visual 2.0 motion: the active indicator is a small accent bar
        # that SLIDES between tabs (animated geometry) instead of the old
        # static per-button border-top. Free-floating child (no layout).
        self._active_id = int(Tab.DIALPAD)
        self._indicator = QFrame(self)
        self._indicator.setObjectName("TabIndicator")
        self._indicator.setFixedHeight(3)
        self._indicator.raise_()

    def _indicator_rect(self, tab_id: int):
        from PySide6.QtCore import QRect

        btn = self._buttons.get(int(tab_id))
        if btn is None:
            return QRect(0, 0, 0, 3)
        w = min(max(btn.width() - 36, 28), 48)
        x = btn.x() + (btn.width() - w) // 2
        return QRect(x, 0, w, 3)

    def _move_indicator(self, tab_id: int, animate: bool = True) -> None:
        rect = self._indicator_rect(tab_id)
        if rect.width() <= 0:
            return
        if animate:
            from noc_beam.ui import motion
            motion.slide_geometry(self._indicator, rect, 180)
        else:
            self._indicator.setGeometry(rect)

    def showEvent(self, event) -> None:  # noqa: N802, ANN001
        super().showEvent(event)
        self._move_indicator(self._active_id, animate=False)

    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001
        super().resizeEvent(event)
        self._move_indicator(self._active_id, animate=False)

    def _on_id_clicked(self, tab_id: int) -> None:
        self._active_id = int(tab_id)
        self._move_indicator(tab_id)
        self.tab_changed.emit(tab_id)

    def select(self, tab: int) -> None:
        btn = self._buttons.get(int(tab))
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
            self._active_id = int(tab)
            self._move_indicator(int(tab))
            self.tab_changed.emit(int(tab))

    def set_badge(self, tab: int, count: int) -> None:
        """Show a small unread-count badge on a tab. count<=0 clears it.
        Implemented by appending the count to the button text inside a
        unicode bullet so it reads as a chip without needing a custom
        paintEvent. Intentionally simple."""
        btn = self._buttons.get(int(tab))
        if btn is None:
            return
        # Stash the original label once so we can reset it.
        if not hasattr(btn, "_base_label"):
            btn._base_label = btn.text()  # type: ignore[attr-defined]
        base = btn._base_label  # type: ignore[attr-defined]
        if count > 0:
            shown = 9 if count > 9 else count
            btn.setText(f"{base}  •{shown}")
            btn.setProperty("badged", True)
        else:
            btn.setText(base)
            btn.setProperty("badged", False)
        btn.style().unpolish(btn)
        btn.style().polish(btn)
