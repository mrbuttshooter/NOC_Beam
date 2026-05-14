"""NOC_Beam QApplication bootstrap."""
from __future__ import annotations

import logging
import sys
from importlib import resources
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from noc_beam import __app_name__
from noc_beam.logging_setup import setup_logging
from noc_beam.ui.main_window import MainWindow

log = logging.getLogger(__name__)


def _load_stylesheet() -> str:
    try:
        qss = resources.files("noc_beam.ui.resources").joinpath("dark.qss").read_text(
            encoding="utf-8"
        )
        return qss
    except Exception:
        log.warning("Could not load stylesheet")
        return ""


def _load_icon() -> QIcon:
    # Look for an icon next to the package or in resources
    here = Path(__file__).resolve().parent
    candidates = [
        here / "ui" / "resources" / "icon.ico",
        here.parent.parent.parent / "assets" / "icon.ico",
    ]
    for p in candidates:
        if p.exists():
            return QIcon(str(p))
    return QIcon()


def run(argv: list[str]) -> int:
    setup_logging()
    log.info("Starting %s", __app_name__)

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    QApplication.setApplicationName(__app_name__)
    QApplication.setOrganizationName(__app_name__)

    app = QApplication(argv)
    app.setWindowIcon(_load_icon())

    qss = _load_stylesheet()
    if qss:
        app.setStyleSheet(qss)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(run(sys.argv))
