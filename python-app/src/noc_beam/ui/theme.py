"""Stylesheet swap helper.

apply_theme(app, high_contrast) loads dark-hc.qss when high_contrast
is true, else dark.qss, and pushes it into the QApplication. Used at
startup (app.py) and live whenever Settings -> Appearance changes.

Falls back gracefully: if dark-hc.qss isn't present the call quietly
keeps the existing stylesheet rather than blanking the UI.
"""
from __future__ import annotations

import logging
from importlib import resources

from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)


def load_theme_qss(high_contrast: bool) -> str:
    """Returns the QSS text for the chosen theme, or '' on failure."""
    name = "dark-hc.qss" if high_contrast else "dark.qss"
    try:
        return resources.files("noc_beam.ui.resources").joinpath(name).read_text(
            encoding="utf-8"
        )
    except Exception:
        log.warning("Could not load stylesheet %s", name, exc_info=True)
        return ""


def apply_theme(app: QApplication, high_contrast: bool) -> None:
    qss = load_theme_qss(high_contrast)
    if qss:
        app.setStyleSheet(qss)
