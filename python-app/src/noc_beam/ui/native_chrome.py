"""Native window chrome (Win32 DWM) helpers.

Qt stylesheets stop at the client area: the native Windows title bar stays
white even when the app theme is dark, leaving a glaring white strip above
the slate UI. DWM exposes a per-window switch for dark chrome --
``DwmSetWindowAttribute(DWMWA_USE_IMMERSIVE_DARK_MODE)`` -- which this module
wraps and applies to every top-level window via a QApplication event filter.

Attribute numbering moved between Windows 10 builds: 20 is the documented
value from Win10 20H1 (build 19041) onward; builds 1809..1909 used the
undocumented 19. We try 20 first and fall back to 19, so older Win10 still
gets dark bars where the OS supports them at all.

Everything here is best-effort and Win32-only: on other platforms (or if DWM
refuses) the helpers are silent no-ops -- a light title bar is cosmetic, never
worth failing startup over.
"""
from __future__ import annotations

import logging
import sys
from typing import Callable

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

# DWMWA_USE_IMMERSIVE_DARK_MODE. 20 since Win10 20H1; 19 on 1809..1909.
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19


def apply_dark_title_bar(widget: QWidget, enabled: bool) -> bool:
    """Ask DWM for dark (or light) native title-bar chrome on `widget`.

    Returns True if DWM accepted the attribute, False on any failure or on
    non-Windows platforms. Never raises -- chrome color is cosmetic.

    Must be called after the widget has a native window handle. Qt creates
    one lazily; calling ``winId()`` forces creation, which is fine for
    top-level windows about to be shown (the only ones we target).
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import ctypes.wintypes

        hwnd = int(widget.winId())
        if not hwnd:
            return False

        dwmapi = ctypes.WinDLL("dwmapi")
        # BOOL by reference, per the DwmSetWindowAttribute contract
        # (pvAttribute points at a BOOL of cbAttribute bytes).
        value = ctypes.wintypes.BOOL(1 if enabled else 0)
        for attr in (_DWMWA_USE_IMMERSIVE_DARK_MODE, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
            # S_OK (0) means accepted; nonzero HRESULT -> try the older
            # attribute number used by pre-20H1 Win10 builds.
            hr = dwmapi.DwmSetWindowAttribute(
                ctypes.wintypes.HWND(hwnd),
                ctypes.wintypes.DWORD(attr),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            if hr == 0:
                return True
        log.debug(
            "DwmSetWindowAttribute rejected dark-mode attrs 20 and 19 "
            "for hwnd=%#x (hr=%#010x)", hwnd, hr & 0xFFFFFFFF,
        )
        return False
    except Exception:
        # ctypes / DWM edge (dwmapi missing on Server Core, weird hwnd, ...).
        # One log line as breadcrumb; never propagate.
        log.exception("apply_dark_title_bar failed")
        return False


class DarkTitleBarFilter(QObject):
    """QApplication event filter that darkens native chrome on every window.

    Watches for ``QEvent.Show`` on top-level widgets (``isWindow()``) and
    applies :func:`apply_dark_title_bar` when the active theme is dark. The
    theme is re-checked on every Show via the injected ``theme_provider``
    (a callable returning the current theme string, e.g. re-reading
    settings), so dialogs opened *after* a runtime theme switch get the
    right chrome.

    Known limitation (accepted): windows already on screen when the theme
    changes at runtime keep their old bar color until they are re-shown or
    recreated -- DWM repaints on the next Show, and we deliberately don't
    track/re-stamp live windows.
    """

    def __init__(
        self,
        theme_provider: Callable[[], str],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme_provider = theme_provider

    @staticmethod
    def _is_dark(theme: str) -> bool:
        # "dark" and variants like "dark-hc" all want dark chrome.
        return isinstance(theme, str) and theme.lower().startswith("dark")

    def eventFilter(self, obj, event):  # noqa: ANN001, N802
        try:
            if (
                event.type() == QEvent.Type.Show
                and isinstance(obj, QWidget)
                and obj.isWindow()
            ):
                theme = "dark"
                try:
                    theme = self._theme_provider()
                except Exception:
                    # A broken provider shouldn't kill chrome styling --
                    # dark is the app default, so fail toward it.
                    log.exception("theme_provider raised; assuming dark chrome")
                if self._is_dark(theme):
                    apply_dark_title_bar(obj, True)
        except Exception:
            # Event filters run inside the Qt event loop -- never let an
            # exception escape (it would abort event delivery).
            log.exception("DarkTitleBarFilter raised")
        return False  # observe only; never consume the event
