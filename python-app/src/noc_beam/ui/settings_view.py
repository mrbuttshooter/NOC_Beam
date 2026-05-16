"""Settings destination -- left nav sidebar + stacked body + sticky Apply.

Builds a SettingsDialog so we can reuse its `_build_*_tab()` factories,
then re-parents each tab's body widget into a QStackedWidget driven by
a left-side QListWidget. The dialog instance stays alive (apply_to()
walks its widgets) but is never shown -- it acts as a model holder.

Pages, in nav order: Audio · Codecs · Appearance · Advanced.

The Apply button lives in a sticky footer below the body, mirroring the
.settings-footer pattern in the mockup. Apply emits apply_requested
with the codec map, identical to the previous flow.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from noc_beam.config.store import GlobalSettings
from noc_beam.ui.settings_dialog import SettingsDialog


# Order matches the SettingsDialog tab order; index drives the stack.
_PAGES: tuple[tuple[str, str], ...] = (
    ("Audio",      "audio devices, echo cancel, clock rate"),
    ("Codecs",     "negotiated audio codec priorities"),
    ("Appearance", "high-contrast theme, reduced motion"),
    ("Advanced",   "SIP port, log level"),
)


class SettingsView(QWidget):
    apply_requested = Signal(dict)  # codec_map produced by SettingsDialog.apply_to

    def __init__(self, settings: GlobalSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        # Underlying dialog -- never shown. Owns all the form widgets so
        # apply_to() reads off them directly. Parented to self so it
        # gets cleaned up when this view is destroyed (otherwise the
        # dialog leaks every time the view is shown).
        self._dialog = SettingsDialog(settings, parent=self)

        # Title bar across the top of the destination
        title = QLabel("Settings")
        title.setObjectName("ViewTitle")
        title_row = QHBoxLayout()
        title_row.setContentsMargins(16, 16, 16, 12)
        title_row.addWidget(title)
        title_row.addStretch(1)

        # Nav sidebar
        self.nav = QListWidget()
        self.nav.setObjectName("SettingsNav")
        self.nav.setFixedWidth(220)
        for label, _hint in _PAGES:
            item = QListWidgetItem(label)
            self.nav.addItem(item)

        # Body stack -- pull each pane body out of the dialog. The
        # dialog renamed _build_*_tab -> _build_*_pane in the sidebar
        # refactor; using the old names was an AttributeError on every
        # instantiation of SettingsView (dead-on-arrival regression).
        self.body_stack = QStackedWidget()
        self.body_stack.addWidget(self._wrap(self._dialog._build_audio_pane(),
                                            "Audio", _PAGES[0][1]))
        self.body_stack.addWidget(self._wrap(self._dialog._build_codec_pane(),
                                            "Codecs", _PAGES[1][1]))
        self.body_stack.addWidget(self._wrap(self._dialog._build_appearance_pane(),
                                            "Appearance", _PAGES[2][1]))
        self.body_stack.addWidget(self._wrap(self._dialog._build_advanced_pane(),
                                            "Advanced", _PAGES[3][1]))

        body_holder = QFrame()
        body_holder.setObjectName("SettingsBody")
        body_l = QVBoxLayout(body_holder)
        body_l.setContentsMargins(0, 0, 0, 0)
        body_l.addWidget(self.body_stack)

        # Sticky footer with Apply
        footer = QFrame()
        footer.setObjectName("SettingsFooter")
        f_l = QHBoxLayout(footer)
        f_l.setContentsMargins(16, 10, 16, 10)
        f_l.setSpacing(8)
        f_l.addStretch(1)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setObjectName("PrimaryAction")
        self.apply_btn.clicked.connect(self._on_apply)
        f_l.addWidget(self.apply_btn)

        # Compose
        nav_body = QHBoxLayout()
        nav_body.setContentsMargins(0, 0, 0, 0)
        nav_body.setSpacing(0)
        nav_body.addWidget(self.nav)
        nav_body.addWidget(body_holder, 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(title_row)
        outer.addLayout(nav_body, 1)
        outer.addWidget(footer)

        self.nav.currentRowChanged.connect(self.body_stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

    # ------------------------------------------------------------------
    @staticmethod
    def _wrap(body: QWidget, title: str, hint: str) -> QWidget:
        """Per-page header + the dialog tab body inside a padded container."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("ViewTitle")
        hint_lbl = QLabel(hint)
        hint_lbl.setObjectName("ViewHint")
        hint_lbl.setWordWrap(True)
        layout.addWidget(title_lbl)
        layout.addWidget(hint_lbl)
        layout.addSpacing(4)
        layout.addWidget(body, 1)
        return page

    def _on_apply(self) -> None:
        codec_map = self._dialog.apply_to(self._settings)
        self.apply_requested.emit(codec_map)
