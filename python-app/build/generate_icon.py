"""Render the canonical mark SVG to a multi-size Windows .ico.

Run from the repo root:
    .venv\\Scripts\\python.exe python-app\\build\\generate_icon.py

Sizes baked: 16, 24, 32, 48, 64, 128, 256 — covers tray, taskbar,
Alt-Tab, file-explorer, and installer chrome.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

SIZES = (16, 24, 32, 48, 64, 128, 256)
HERE = Path(__file__).resolve().parent
RESOURCES = HERE.parent / "src" / "noc_beam" / "ui" / "resources"
SRC_SVG = RESOURCES / "logo-mark.svg"
DST_ICO = RESOURCES / "icon.ico"


def render(svg_bytes: bytes, px: int) -> Image.Image:
    img = QImage(QSize(px, px), QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(svg_bytes))
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()
    buf = io.BytesIO()
    img.save_to_bytes = None  # mypy quiet
    # QImage -> PNG bytes via Qt's writer, then load into PIL.
    from PySide6.QtCore import QBuffer

    qbuf = QBuffer()
    qbuf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(qbuf, "PNG")
    qbuf.close()
    return Image.open(io.BytesIO(bytes(qbuf.data())))


def main() -> int:
    if not SRC_SVG.exists():
        print(f"missing source SVG: {SRC_SVG}")
        return 1
    app = QGuiApplication.instance() or QGuiApplication([])
    _ = app  # keep alive
    svg = SRC_SVG.read_bytes()
    frames = [render(svg, px) for px in SIZES]
    base = frames[-1].copy()
    base.save(DST_ICO, format="ICO", sizes=[(px, px) for px in SIZES])
    print(f"wrote {DST_ICO}  ({DST_ICO.stat().st_size} bytes, {len(SIZES)} sizes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
