"""FAS detection ONNX models.

The .onnx files in this directory are NOT committed to git. They are fetched
by build/fetch_fas_models.py from the URLs pinned in build/MODELS.lock, and
bundled into the PyInstaller --onedir distribution.

At runtime, code uses model_path(name) below to locate them whether running
from source (this directory) or from a bundled exe (PyInstaller _MEIPASS).
"""
from __future__ import annotations

import sys
from pathlib import Path


def model_path(filename: str) -> Path:
    """Return the absolute path to a bundled model file.

    Works both from source (returns this package dir) and from a PyInstaller
    bundle (returns sys._MEIPASS/noc_beam/audio/models/...).
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "noc_beam" / "audio" / "models" / filename
    return Path(__file__).parent / filename
