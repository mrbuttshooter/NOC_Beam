"""Centralised logging for NOC_Beam.

Writes to %APPDATA%/NOC_Beam/logs/noc_beam.log plus stderr.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from noc_beam.config.paths import log_dir

_INITIALISED = False


def setup_logging(level: int = logging.INFO) -> None:
    global _INITIALISED
    if _INITIALISED:
        return
    _INITIALISED = True

    log_path: Path = log_dir() / "noc_beam.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)

    logging.getLogger("noc_beam").info("Logging initialised → %s", log_path)
