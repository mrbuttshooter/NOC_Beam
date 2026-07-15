"""NOC_Beam web-UI POC -- custom-designed HTML/CSS Dial surface in a
native pywebview window (WebView2). No component library, no server:
the page ships with the app and Python is the backend via js_api.

Run:  .venv\\Scripts\\python.exe prototype\\webui_poc.py
"""
from __future__ import annotations

from pathlib import Path

import webview


class Api:
    """Bridge the UI calls into Python (the real app would route these
    into the existing call manager / PJSIP endpoint)."""

    def __init__(self) -> None:
        self._window: webview.Window | None = None

    def attach(self, window: webview.Window) -> None:
        self._window = window

    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()


def main() -> None:
    api = Api()
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "index"
    page = Path(__file__).parent / "webui" / f"{name}.html"
    window = webview.create_window(
        "NOC_Beam",
        url=page.as_uri(),
        width={'console': 980, 'concepts': 1260}.get(name, 420),
        height={'console': 680, 'concepts': 820}.get(name, 760),
        frameless=True,
        easy_drag=False,   # drag comes from the .pywebview-drag-region class
        js_api=api,
        background_color="#14161d",
    )
    api.attach(window)
    webview.start()


if __name__ == "__main__":
    main()
