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
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMainWindow, QMenu

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


def _file_stamp(path: Path) -> tuple[int, int]:
    """(mtime_ns, size) of a data file, (0, 0) if missing. Used to skip
    re-reading + re-pushing history/contacts when nothing changed --
    every tab switch used to re-read the JSON from disk, re-serialize
    up to 200 rows and re-render the list that was already on screen."""
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def _history_stamp() -> tuple[int, int]:
    try:
        from noc_beam.config.history import history_file
        return _file_stamp(history_file())
    except Exception:
        return (0, 0)


def _contacts_stamp() -> tuple[int, int]:
    try:
        from noc_beam.config.contacts import contacts_file
        return _file_stamp(contacts_file())
    except Exception:
        return (0, 0)


class _WebEngineView(QWebEngineView):
    """QWebEngineView that suppresses QtWebEngine's stock browser context
    menu (Back / Forward / Reload / Save page / View source -- wrong for a
    product; owner feedback 2026-07-16.3).

    * Editable target (dial input, search boxes, dropdown filters): show a
      minimal Cut / Copy / Paste / Select-all menu built from the page's own
      web actions (styled by the app-wide dark QSS).
    * Anything else: swallow the event entirely. The page's JS already
      preventDefault()s non-editable right-clicks and opens the app ☰ menu at
      the cursor, so no menu is wanted here -- this is the belt-and-braces
      kill in case a build's JS handler didn't run.
    """

    _EDIT_ACTIONS = (
        QWebEnginePage.WebAction.Cut,
        QWebEnginePage.WebAction.Copy,
        QWebEnginePage.WebAction.Paste,
        QWebEnginePage.WebAction.SelectAll,
    )

    def contextMenuEvent(self, event):  # noqa: N802, ANN001
        editable = False
        try:
            req = self.lastContextMenuRequest()
            if req is not None:
                editable = bool(req.isContentEditable())
        except Exception:
            editable = False
        if editable:
            try:
                menu = QMenu(self)
                page = self.page()
                for web_action in self._EDIT_ACTIONS:
                    act = page.action(web_action)
                    if act is not None:
                        menu.addAction(act)
                if menu.actions():
                    menu.popup(event.globalPos())
            except Exception:
                log.debug("edit context menu build failed", exc_info=True)
        # Editable or not, never let the default browser menu surface.
        event.accept()


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
        self._view = _WebEngineView(self)
        self.setCentralWidget(self._view)
        self._channel = QWebChannel(self)
        self._bridge = WebBridge(phone, self, parent=self)
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)
        self._view.loadFinished.connect(self._on_load_finished)
        self._view.setUrl(QUrl.fromLocalFile(str(_ASSETS / "index.html")))

        # ---- Live RX/TX meter push (owner feedback 2026-07-16.3) --------
        # A ~6.7 Hz timer bridges PhoneShell's AudioStrip meter values (fed by
        # its own 200 ms pjsua2 poll) to the web call cards while a call is
        # live. Armed on call_added, self-stops when no call is active -- zero
        # pushes when idle. We read the AudioStrip's already-computed values
        # rather than re-touch pjsua2, so there's no duplicate native polling.
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(150)
        self._level_timer.timeout.connect(self._push_levels)
        self._last_levels_key: tuple[int, int, int] | None = None
        # Last pushed data-file stamps (see _file_stamp) so tab switches
        # don't re-push identical history/contacts payloads.
        self._pushed_history_stamp: tuple[int, int] | None = None
        self._pushed_contacts_stamp: tuple[int, int] | None = None
        # Pop-out windows: stamp at last reload, so re-opening a pop-out
        # doesn't rebuild every row when the file is unchanged.
        self._popout_stamps: dict[str, tuple[int, int]] = {}

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
        # Idle prewarm of the heavy Qt aux windows (Test runner, Trace) so
        # their first open paints as fast as a repeat open.
        QTimer.singleShot(1500, self._prewarm_aux_windows)

    def _prewarm_aux_windows(self) -> None:
        fn = getattr(self._phone, "prewarm_aux_windows", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                log.debug("aux window prewarm failed", exc_info=True)
        # Pre-load the pop-out views (hidden shell children) so their first
        # open skips the 90-110 ms row rebuild; _popout then sees a matching
        # stamp and only reloads when the file actually changed.
        for attr, view_attr, stamp in (
            ("_history_window", "history_view", _history_stamp),
            ("_contacts_window", "contacts_view", _contacts_stamp),
            ("_favorites_window", "favorites_view", _contacts_stamp),
        ):
            view = getattr(self._phone, view_attr, None)
            reload_fn = getattr(view, "reload", None)
            if callable(reload_fn):
                try:
                    s = stamp()
                    reload_fn()
                    self._popout_stamps[attr] = s
                except Exception:
                    log.debug("pop-out prewarm failed for %s", attr, exc_info=True)

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

    def _apply_levels(self, payload: dict) -> None:
        self._js("window.nb && nb.levels(" + json.dumps(payload) + ");")

    # ------------------------------------------------------------------
    # Live RX/TX meters (Python -> UI, out-of-band high-frequency push)
    # ------------------------------------------------------------------
    def _arm_level_timer(self, *_args) -> None:
        """A new call materialised -> start pushing meter levels."""
        try:
            if not self._level_timer.isActive():
                self._last_levels_key = None
                self._level_timer.start()
        except Exception:
            log.debug("level timer arm failed", exc_info=True)

    def _push_levels(self) -> None:
        """Read the AudioStrip's current RX/TX meter values + the audio-focused
        call id and push them to the page. Stops (after a single zeroing push)
        once no call is active."""
        p = self._phone
        try:
            active = p.calls.active()
        except Exception:
            active = []
        if not active:
            # Final zeroing push so the bars empty, then idle the timer.
            if self._last_levels_key != (-1, 0, 0):
                self._apply_levels({"id": -1, "rx": 0, "tx": 0})
                self._last_levels_key = (-1, 0, 0)
            try:
                self._level_timer.stop()
            except Exception:
                pass
            return
        from noc_beam.webui.serializers import clamp_level

        cid = getattr(p, "_selected_call_id", None)
        rx = tx = 0
        try:
            audio = getattr(p, "audio", None)
            if audio is not None:
                rx = clamp_level(audio.rx_bar.value())
                tx = clamp_level(audio.tx_bar.value())
        except Exception:
            rx = tx = 0
        key = (int(cid) if cid is not None else -1, rx, tx)
        # Skip redundant pushes (source poll is 5 Hz; we tick faster) so we
        # don't spam runJavaScript with identical payloads.
        if key == self._last_levels_key:
            return
        self._last_levels_key = key
        self._apply_levels({"id": key[0], "rx": rx, "tx": tx})

    # ------------------------------------------------------------------
    # State pushes (Python -> UI)
    # ------------------------------------------------------------------
    def push_all(self) -> None:
        self._pushed_history_stamp = _history_stamp()
        self._pushed_contacts_stamp = _contacts_stamp()
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

    def push_history(self, *_args, force: bool = False) -> None:
        """Push the history list. Skipped when the history file is
        unchanged since the last push (tab switches call this on every
        open); `force=True` bypasses the check (call ended -> CDR written)."""
        stamp = _history_stamp()
        if not force and stamp == self._pushed_history_stamp:
            return
        self._pushed_history_stamp = stamp
        self._apply({"history": self._history_payload()})

    def push_contacts(self, *_args, force: bool = False) -> None:
        stamp = _contacts_stamp()
        if not force and stamp == self._pushed_contacts_stamp:
            return
        self._pushed_contacts_stamp = stamp
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
        # A new call arms the RX/TX meter push loop (self-stops when idle).
        p.calls.call_added.connect(self._arm_level_timer)
        # Recents + history refresh after a call ends. Deferred one event-loop
        # tick so PhoneShell._maybe_write_cdr (which appends the CDR on the
        # same synchronous burst) has finished before we re-read the file.
        p.calls.call_removed.connect(lambda *_: QTimer.singleShot(0, self.push_recents))
        p.calls.call_removed.connect(
            lambda *_: QTimer.singleShot(0, lambda: self.push_history(force=True))
        )
        # Contacts edited in the Qt manage window -> refresh the web tab.
        try:
            p.contacts_view.contact_saved.connect(
                lambda *_: QTimer.singleShot(0, lambda: self.push_contacts(force=True))
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
        # The hosted views come out of the hidden PhoneShell's QStackedWidget,
        # where every non-current page is EXPLICITLY hidden (the stack calls
        # hide() on them). setCentralWidget -> setParent PRESERVES that
        # explicit-hidden flag, so without this show() the pop-out rendered a
        # bare blank canvas (owner P0 bug, phase 3.1).
        view.show()
        # Fresh data on open: with the shell hidden, none of the
        # tab-switch/visibility paths that used to refresh these views fire.
        # Skipped when the backing file is unchanged since the last reload
        # (each reload tears down + rebuilds every row widget: 65-110 ms
        # measured for a small history -- a visible stutter for nothing).
        stamp = _history_stamp() if attr == "_history_window" else _contacts_stamp()
        reload_fn = getattr(view, "reload", None)
        if callable(reload_fn) and self._popout_stamps.get(attr) != stamp:
            try:
                reload_fn()
                self._popout_stamps[attr] = stamp
            except Exception:
                log.exception("pop-out reload failed for %s", title)
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
