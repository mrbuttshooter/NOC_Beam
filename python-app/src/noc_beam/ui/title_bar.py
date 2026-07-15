"""Top title bar.

Wordmark on the left, active-account chip in the middle, global dial
bar on the right. Kept inside the central widget rather than replacing
the OS chrome -- frameless windows on Windows need careful Aero Snap
handling that's out of scope for the v2 first cut.

Public API:
- `set_accounts(list[(account_id, label)])` -- populates the chip menu
- `set_active_account(account_id)`           -- moves the chip selection
- `active_account_id` property               -- returns the current pick
- Signals: `dial_requested(str)`, `active_account_changed(str)`
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QSizePolicy,
    QToolButton,
    QWidget,
)


TITLE_BAR_H = 44
WORDMARK_PX = 18
RESOURCES = Path(__file__).resolve().parent / "resources"


def _dot_pixmap(color_hex: str, px: int = 12) -> QPixmap:
    """A filled circle with a soft halo, used as the chip's status dot."""
    pix = QPixmap(QSize(px, px))
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    halo = QColor(color_hex)
    halo.setAlpha(54)  # ~0.21 alpha to mimic the box-shadow halo
    painter.setBrush(halo)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(0, 0, px, px)
    painter.setBrush(QColor(color_hex))
    inset = px // 4
    painter.drawEllipse(inset, inset, px - 2 * inset, px - 2 * inset)
    painter.end()
    return pix


def _wordmark_pixmap(height_px: int = WORDMARK_PX, color: str = "#E3E9F2") -> QPixmap:
    """Render the title-bar wordmark SVG to a pixmap at `height_px` height."""
    svg_path = RESOURCES / "logo-wordmark.svg"
    if not svg_path.exists():
        return QPixmap()
    raw = svg_path.read_text(encoding="utf-8").replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(raw.encode("utf-8")))
    aspect = renderer.defaultSize().width() / max(renderer.defaultSize().height(), 1)
    width_px = int(height_px * aspect)
    pix = QPixmap(QSize(width_px, height_px))
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return pix


class TitleBar(QFrame):
    dial_requested = Signal(str)
    active_account_changed = Signal(str)
    active_account_clicked = Signal()  # opens the Accounts destination

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(TITLE_BAR_H)

        self._accounts: list[tuple[str, str]] = []
        self._active_id: str = ""

        # ---- Wordmark
        self.wordmark = QLabel()
        self.wordmark.setObjectName("Wordmark")
        pm = _wordmark_pixmap()
        if not pm.isNull():
            self.wordmark.setPixmap(pm)
        else:
            self.wordmark.setText("noc_beam")
        self.wordmark.setFixedHeight(TITLE_BAR_H)

        # ---- Active account chip (button with dropdown menu).
        # Carries a coloured status pixmap (rendered, not a glyph) so the
        # dot reads sharp at any DPI and respects the registration state.
        self.chip = QToolButton(self)
        self.chip.setObjectName("AccountChip")
        self.chip.setText("No account")
        self.chip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.chip.setIcon(QIcon(_dot_pixmap("#7C889E")))
        self.chip.setIconSize(QSize(10, 10))
        self.chip.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.chip.setMenu(QMenu(self.chip))
        self.chip.clicked.connect(self.active_account_clicked.emit)

        # ---- Dial bar (Ctrl+K focuses it). Wrapped in a frame so the
        # mockup's "sip:" prefix + Ctrl+K hint chip can sit inside the
        # bordered container alongside the input.
        dial_frame = QFrame(self)
        dial_frame.setObjectName("DialBar")
        dial_frame.setFixedHeight(28)
        dial_frame.setMaximumWidth(420)
        dial_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        df = QHBoxLayout(dial_frame)
        df.setContentsMargins(10, 0, 8, 0)
        df.setSpacing(8)
        prefix = QLabel("sip:", dial_frame)
        prefix.setObjectName("DialBarPrefix")
        df.addWidget(prefix)
        self.dial = QLineEdit(dial_frame)
        self.dial.setObjectName("DialBarInput")
        self.dial.setFrame(False)
        self.dial.setPlaceholderText("alice@example.com  or  number")
        self.dial.setClearButtonEnabled(True)
        self.dial.returnPressed.connect(self._on_return)
        df.addWidget(self.dial, 1)
        kbd = QLabel("Ctrl+K", dial_frame)
        kbd.setObjectName("DialBarKbd")
        df.addWidget(kbd)
        self._dial_frame = dial_frame

        focus_dial = QAction(self)
        focus_dial.setShortcut(QKeySequence("Ctrl+K"))
        focus_dial.triggered.connect(self._focus_dial)
        self.addAction(focus_dial)

        # ---- Layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(12)
        layout.addWidget(self.wordmark)
        layout.addSpacing(4)
        layout.addWidget(self.chip)
        layout.addStretch(1)
        layout.addWidget(self._dial_frame)

    # ------------------------------------------------------------------
    def _focus_dial(self) -> None:
        self.dial.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.dial.selectAll()

    def _on_return(self) -> None:
        target = self.dial.text().strip()
        if not target:
            return
        self.dial_requested.emit(target)
        self.dial.clear()

    # ------------------------------------------------------------------
    def set_accounts(self, accounts: list[tuple[str, str]]) -> None:
        """`accounts` is list of (account_id, display_label). Order = menu order."""
        self._accounts = list(accounts)
        menu = QMenu(self.chip)
        if not accounts:
            empty = menu.addAction("No accounts")
            empty.setEnabled(False)
        else:
            for acc_id, label in accounts:
                act = menu.addAction(label)
                act.triggered.connect(lambda _checked=False, aid=acc_id: self._select(aid))
        menu.addSeparator()
        manage = menu.addAction("Manage accounts…")
        manage.triggered.connect(self.active_account_clicked.emit)
        # setMenu() doesn't free the previous menu -- it stays parented to
        # the chip forever. Every set_accounts() call (fired on account
        # add/edit/remove) leaked a QMenu + actions. Delete the old one.
        old_menu = self.chip.menu()
        self.chip.setMenu(menu)
        if old_menu is not None:
            old_menu.deleteLater()
        # Re-select the active account if it still exists, else first
        ids = [a for a, _ in accounts]
        if self._active_id in ids:
            self._set_chip_text(self._active_id)
        elif ids:
            self._select(ids[0])
        else:
            self._active_id = ""
            self.chip.setText("No account")

    def set_active_account(self, account_id: str) -> None:
        if account_id != self._active_id:
            self._select(account_id)

    @property
    def active_account_id(self) -> str:
        return self._active_id

    # ------------------------------------------------------------------
    def _select(self, account_id: str) -> None:
        self._active_id = account_id
        self._set_chip_text(account_id)
        self.active_account_changed.emit(account_id)

    def _set_chip_text(self, account_id: str) -> None:
        label = next((lbl for aid, lbl in self._accounts if aid == account_id), account_id)
        self.chip.setText(label)
        # Optimistic green dot whenever a real account is selected; the
        # actual registration code is broadcast via registration_changed
        # and a future tick can refine this per-account.
        self.chip.setIcon(QIcon(_dot_pixmap("#57C98B")))

    # ------------------------------------------------------------------
    def set_chip_status(self, level: str) -> None:
        """Recolour the chip dot. level in: ok / warn / danger / muted."""
        color = {
            "ok": "#57C98B",
            "warn": "#E8B34B",
            "danger": "#E0575B",
            "muted": "#7C889E",
        }.get(level, "#7C889E")
        self.chip.setIcon(QIcon(_dot_pixmap(color)))
