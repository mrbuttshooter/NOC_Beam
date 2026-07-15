"""WebShell -- the visible compact web softphone window.

Architecture (see docs/redesign/WEB-SOFTPHONE-brief.md):
  * A frameless QMainWindow hosting a full-window QWebEngineView that renders
    the approved prototype (webui/assets/index.html) with all data dynamic.
  * A QWebChannel exposes `bridge` (webui/bridge.py) to the page.
  * The real call orchestration lives in a PhoneShell instantiated WITHOUT
    being shown -- the "logic host" strategy for phase 1. WebShell subscribes
    to the same CallManager / sip_events signals PhoneShell reacts to and
    mirrors state into the page via page.runJavaScript("nb.apply(...)").

Phase 2 (noted for the next engineer): extract a headless CallSession service
from PhoneShell so the hidden-window host can be retired.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMainWindow

from noc_beam import __app_name__
from noc_beam.sip.events import sip_events
from noc_beam.webui.bridge import WebBridge
from noc_beam.webui.serializers import (
    serialize_accounts,
    serialize_calls,
    serialize_contacts,
    serialize_history,
    serialize_recents,
    serialize_suppliers,
)

log = logging.getLogger(__name__)

_ASSETS = Path(__file__).resolve().parent / "assets"


class WebShell(QMainWindow):
    # Hit-test codes for the frameless resize band (WinUser.h) -- same table
    # PhoneShell uses so edge-resize / Aero Snap keep working without a frame.
    _HT_EDGES = {
        (True, False, True, False): 13,   # top-left
        (False, True, True, False): 14,   # top-right
        (True, False, False, True): 16,   # bottom-left
        (False, True, False, True): 17,   # bottom-right
        (True, False, False, False): 10,  # left
        (False, True, False, False): 11,  # right
        (False, False, True, False): 12,  # top
        (False, False, False, True): 15,  # bottom
    }

    def __init__(self, phone) -> None:
        super().__init__()
        self._phone = phone
        self._really_quitting = False
        self._dwm_polished = False
        self._page_ready = False

        self.setWindowTitle(__app_name__)
        # Owner-locked compact footprint. Phase-2 owner feedback: tighter
        # 360x560 default (keypad shrunk to ~31px keys to fit).
        self.resize(360, 560)
        self.setMinimumWidth(360)
        self.setMinimumHeight(460)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowMinMaxButtonsHint
        )

        # ---- Web view + channel -----------------------------------------
        self._view = QWebEngineView(self)
        self.setCentralWidget(self._view)
        self._channel = QWebChannel(self)
        self._bridge = WebBridge(phone, self, parent=self)
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)
        self._view.loadFinished.connect(self._on_load_finished)
        self._view.setUrl(QUrl.fromLocalFile(str(_ASSETS / "index.html")))

        # ---- Mirror PhoneShell state into the page ----------------------
        self._wire_mirror()
        self._repoint_activation()

    # ------------------------------------------------------------------
    # Page lifecycle
    # ------------------------------------------------------------------
    def _on_load_finished(self, ok: bool) -> None:
        self._page_ready = bool(ok)
        if not ok:
            log.warning("WebShell page failed to load")
            return
        # The page also calls bridge.ready() after the channel handshake;
        # this is a belt-and-braces initial push in case that races.
        QTimer.singleShot(0, self.push_all)

    def _js(self, script: str) -> None:
        if not self._page_ready:
            return
        try:
            self._view.page().runJavaScript(script)
        except Exception:
            log.exception("runJavaScript failed")

    def _apply(self, payload: dict) -> None:
        # ensure_ascii=True escapes U+2028/2029 etc. so the JSON is always a
        # legal JS literal for runJavaScript.
        self._js("window.nb && nb.apply(" + json.dumps(payload) + ");")

    # ------------------------------------------------------------------
    # State pushes (Python -> UI)
    # ------------------------------------------------------------------
    def push_all(self) -> None:
        self._apply(
            {
                "calls": self._calls_payload(),
                "accounts": self._accounts_payload(),
                "suppliers": self._suppliers_payload(),
                "recents": self._recents_payload(),
                "history": self._history_payload(),
                "contacts": self._contacts_payload(),
            }
        )

    def push_history(self, *_args) -> None:
        self._apply({"history": self._history_payload()})

    def push_contacts(self, *_args) -> None:
        self._apply({"contacts": self._contacts_payload()})

    def push_call(self, *_args) -> None:
        self._apply({"calls": self._calls_payload()})

    def push_accounts(self, *_args) -> None:
        self._apply({"accounts": self._accounts_payload()})

    def push_suppliers(self, *_args) -> None:
        self._apply({"suppliers": self._suppliers_payload()})

    def push_recents(self, *_args) -> None:
        self._apply({"recents": self._recents_payload()})

    def push_account_and_supplier(self, *_args) -> None:
        self._apply(
            {
                "accounts": self._accounts_payload(),
                "suppliers": self._suppliers_payload(),
                "calls": self._calls_payload(),
            }
        )

    # ------------------------------------------------------------------
    # Payload builders (read hidden PhoneShell state)
    # ------------------------------------------------------------------
    def _calls_payload(self):
        """ALL active calls (phase-2 multi-call stack), selected-flagged."""
        p = self._phone
        try:
            return serialize_calls(
                p.calls.active(), getattr(p, "_selected_call_id", None)
            )
        except Exception:
            log.exception("_calls_payload failed")
            return []

    def _accounts_payload(self):
        p = self._phone
        try:
            return serialize_accounts(
                list(p.accounts),
                getattr(p, "_active_account_id", ""),
                dict(getattr(p, "_reg_state", {})),
            )
        except Exception:
            log.exception("_accounts_payload failed")
            return {"accounts": [], "activeId": "", "activeLabel": "No account", "activeHealth": "muted"}

    def _suppliers_payload(self):
        p = self._phone
        try:
            all_suppliers = list(getattr(p, "_all_suppliers", []) or [])
            acc = p._selected_account()
            kind = (getattr(acc, "switch_type", "other") or "other").lower() if acc else "other"
            visible = kind in ("teles", "genband") and bool(all_suppliers)
            return serialize_suppliers(
                all_suppliers,
                getattr(p, "_active_supplier_id", ""),
                visible,
            )
        except Exception:
            log.exception("_suppliers_payload failed")
            return {"visible": False, "activeId": "", "suppliers": []}

    def _recents_payload(self):
        try:
            from noc_beam.config.history import load_history

            return serialize_recents(load_history(), limit=10)
        except Exception:
            log.exception("_recents_payload failed")
            return []

    def _history_payload(self):
        try:
            from noc_beam.config.history import load_history

            return serialize_history(load_history(), list(self._phone.accounts), limit=200)
        except Exception:
            log.exception("_history_payload failed")
            return []

    def _contacts_payload(self):
        try:
            from noc_beam.config.contacts import load_contacts

            return serialize_contacts(load_contacts())
        except Exception:
            log.exception("_contacts_payload failed")
            return []

    # ------------------------------------------------------------------
    # Wiring: mirror the same signals PhoneShell reacts to
    # ------------------------------------------------------------------
    def _wire_mirror(self) -> None:
        p = self._phone
        # Call state: any add/update/remove re-pushes the live card.
        p.calls.call_added.connect(self.push_call)
        p.calls.call_updated.connect(self.push_call)
        p.calls.call_removed.connect(self.push_call)
        # Recents + history refresh after a call ends. Deferred one event-loop
        # tick so PhoneShell._maybe_write_cdr (which appends the CDR on the
        # same synchronous burst) has finished before we re-read the file.
        p.calls.call_removed.connect(lambda *_: QTimer.singleShot(0, self.push_recents))
        p.calls.call_removed.connect(lambda *_: QTimer.singleShot(0, self.push_history))
        # Contacts edited in the Qt manage window -> refresh the web tab.
        try:
            p.contacts_view.contact_saved.connect(
                lambda *_: QTimer.singleShot(0, self.push_contacts)
            )
        except Exception:
            log.debug("contact_saved hook unavailable", exc_info=True)
        # Registration health -> account pill dot + switcher.
        ev = sip_events()
        ev.registration_changed.connect(self._on_registration_changed)
        # Account/supplier selection has no dedicated PhoneShell signal, so
        # the bridge slots call self.push_account_and_supplier() themselves
        # after routing into PhoneShell (see WebBridge.select_account).

    def _on_registration_changed(self, *_args) -> None:
        # PhoneShell handles the same signal to update its own chip; we run
        # after it via a 0ms defer so _reg_state is already updated.
        QTimer.singleShot(0, self.push_accounts)

    # ------------------------------------------------------------------
    # Tray + single-instance: raise THIS window, keep PhoneShell hidden
    # ------------------------------------------------------------------
    def _repoint_activation(self) -> None:
        p = self._phone
        tray = getattr(p, "tray", None)
        if tray is None:
            return
        # PhoneShell bound tray.show_requested -> its own _restore_from_tray
        # (which would showNormal() the hidden host and flash it visible).
        # Disconnect that and route restore to THIS window instead.
        try:
            tray.show_requested.disconnect(p._restore_from_tray)
        except Exception:
            # SignalRegistry bound it; disconnecting the bound method still
            # works because it's the same object identity. If it isn't
            # connected (test builds), ignore.
            log.debug("tray.show_requested disconnect skipped", exc_info=True)
        tray.show_requested.connect(self._restore_from_tray)

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------
    # Auxiliary Qt windows: History / Contacts / Favorites (phase-1 pop-outs)
    # ------------------------------------------------------------------
    def _popout(self, attr: str, view, title: str, size: tuple[int, int]):
        """Host an existing PhoneShell view in a standalone window (same
        pattern as PhoneShell._on_open_accounts). The hidden host's tab stack
        is never shown, so lifting the view out of it is invisible + safe."""
        win = getattr(self, attr, None)
        if win is None:
            win = QMainWindow(self)
            win.setWindowTitle(title)
            win.resize(*size)
            win.setCentralWidget(view)
            setattr(self, attr, win)
        win.show()
        win.raise_()
        win.activateWindow()
        return win

    def open_history(self) -> None:
        self._popout("_history_window", self._phone.history_view, "NOC_Beam history", (720, 620))

    def open_contacts(self) -> None:
        self._popout("_contacts_window", self._phone.contacts_view, "NOC_Beam contacts", (720, 620))

    def open_favorites(self) -> None:
        self._popout("_favorites_window", self._phone.favorites_view, "NOC_Beam favorites", (720, 620))

    # ------------------------------------------------------------------
    # Frameless window plumbing
    # ------------------------------------------------------------------
    def showEvent(self, event):  # noqa: N802, ANN001
        super().showEvent(event)
        if not self._dwm_polished:
            self._dwm_polished = True
            try:
                from noc_beam.ui.native_chrome import apply_rounded_corners

                apply_rounded_corners(self)
            except Exception:
                log.debug("rounded-corner polish failed", exc_info=True)

    def nativeEvent(self, event_type, message):  # noqa: N802, ANN001
        try:
            if event_type == b"windows_generic_MSG" and not self.isMaximized():
                import ctypes
                import ctypes.wintypes

                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == 0x0084:  # WM_NCHITTEST
                    from PySide6.QtGui import QCursor

                    pos = self.mapFromGlobal(QCursor.pos())
                    m = 6
                    left = pos.x() <= m
                    right = pos.x() >= self.width() - m
                    top = pos.y() <= m
                    bottom = pos.y() >= self.height() - m
                    if left or right or top or bottom:
                        code = self._HT_EDGES.get((left, right, top, bottom))
                        if code:
                            return True, code
        except Exception:
            log.debug("nativeEvent hit-test failed", exc_info=True)
        return super().nativeEvent(event_type, message)

    # ------------------------------------------------------------------
    # Close-to-tray (mirror PhoneShell.closeEvent behaviour)
    # ------------------------------------------------------------------
    def closeEvent(self, event):  # noqa: N802, ANN001
        p = self._phone
        tray = getattr(p, "tray", None)
        if not self._really_quitting and tray is not None and tray.available:
            event.ignore()
            self.hide()
            return
        # Real quit: tear down the hidden host (drops sip subscribers, stops
        # the endpoint via PhoneShell.closeEvent) then let the app exit.
        try:
            p._really_quitting = True
            p.close()
        except Exception:
            log.exception("hidden PhoneShell close failed during quit")
        super().closeEvent(event)
