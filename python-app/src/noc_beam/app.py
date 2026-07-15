"""NOC_Beam QApplication bootstrap."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from noc_beam import __app_name__
from noc_beam.config.store import load_settings
from noc_beam.crash_handler import install as install_crash_handler
from noc_beam.logging_setup import setup_logging
from noc_beam.ui.phone_shell import PhoneShell
from noc_beam.ui.theme import apply_theme

log = logging.getLogger(__name__)

# Module-level mutex handle. Kept alive for the process lifetime so the
# named mutex stays held until the OS reaps the process. Windows auto-
# releases the handle on process exit; we deliberately do NOT close it.
_SINGLE_INSTANCE_MUTEX = None
_SINGLE_INSTANCE_NAME = "Global\\NOC_Beam_SingleInstance"
_ERROR_ALREADY_EXISTS = 183


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


def _acquire_single_instance_or_exit(argv: list[str]) -> int | None:
    """Attempt to acquire the process-wide single-instance mutex.

    Returns None on success (this is the only instance, continue startup).
    Returns an int exit code if another instance is already running --
    caller should propagate that code out of run().

    On non-Windows we skip entirely: the mutex API is Win32-only and our
    target platform is Windows. POSIX builds would need flock/fcntl, but
    NOC_Beam is shipped only on Windows so adding that surface is dead
    code today.
    """
    global _SINGLE_INSTANCE_MUTEX
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes

        # use_last_error=True gives us a thread-local snapshot of the Win32
        # last-error captured by the FFI thunk immediately after the call.
        # The old code read ctypes.windll.kernel32 + kernel32.GetLastError()
        # as two separate calls: any ctypes-internal Win32 call between
        # CreateMutexW returning and our GetLastError() (allocation,
        # marshalling, etc.) can clobber the thread's last-error, so the
        # 183 check was documented-unreliable and could let a second
        # instance through. Declaring restype/argtypes also stops ctypes
        # from truncating the returned HANDLE on 64-bit.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = (
            ctypes.wintypes.LPVOID,   # lpMutexAttributes (NULL)
            ctypes.wintypes.BOOL,     # bInitialOwner
            ctypes.wintypes.LPCWSTR,  # lpName
        )
        # bInitialOwner=True so the first instance immediately owns it.
        # The handle is intentionally leaked to module scope; process
        # exit releases it. Name is in the Global\ namespace so it works
        # across user sessions on the same machine (terminal services /
        # fast user switching) -- last-writer-wins on accounts.json is a
        # machine-wide concern, not a per-user one.
        _SINGLE_INSTANCE_MUTEX = kernel32.CreateMutexW(None, True, _SINGLE_INSTANCE_NAME)
        err = ctypes.get_last_error()
    except Exception:
        log.exception("Single-instance check failed; allowing startup")
        return None

    if err != _ERROR_ALREADY_EXISTS:
        return None

    # Another instance is already running. The owner double-clicked the
    # shortcut expecting their window to come forward -- so instead of the
    # old "already running, check your tray" QMessageBox (which scolded the
    # user for the app's own choice to live in the tray), poke the first
    # instance over the activation socket so IT raises its window, then exit
    # 0 SILENTLY. Never surface UI from a second instance.
    log.info("NOC_Beam already running; signalling first instance to activate")
    try:
        # QLocalSocket needs an event dispatcher for its blocking waitFor*
        # calls, so a QCoreApplication must exist. We deliberately use the
        # lightweight QCoreApplication (not QApplication) -- this process is
        # about to exit and never builds a GUI. QApplication.instance()
        # returns None here because the mutex check runs before run() builds
        # the real QApplication.
        from PySide6.QtCore import QCoreApplication

        from noc_beam.single_instance import signal_existing_instance

        _ = QCoreApplication.instance() or QCoreApplication(argv)
        if not signal_existing_instance():
            # First instance hung or is still starting up. Nothing more we can
            # safely do -- exit cleanly and silently. The warning is logged
            # inside signal_existing_instance().
            log.warning("Could not signal first instance; exiting second instance silently")
    except Exception:
        # If even the IPC bootstrap fails (no Qt, etc.), the log lines above
        # are our breadcrumb. Still exit cleanly with no dialog.
        log.exception("Failed to signal first instance; exiting second instance silently")
    return 0


def run(argv: list[str]) -> int:
    # Logging setup FIRST so the single-instance refusal below is actually
    # recorded. Previously the mutex check ran before setup_logging(), so
    # in the frozen console=False build the "already running; aborting
    # second instance" log line went to a not-yet-configured root logger
    # and vanished -- the refusal left no trace to diagnose. setup_logging
    # only wires handlers (no PJSIP/Qt side effects), so it's safe to run
    # ahead of the mutex acquire.
    setup_logging()

    # Single-instance guard -- before crash handler or any PJSIP/Qt
    # construction. Two NOC_Beam processes both writing accounts.json +
    # call_history.json silently lose CDRs (last-writer-wins), and PJSIP
    # itself wants a singleton process.
    _existing = _acquire_single_instance_or_exit(argv)
    if _existing is not None:
        return _existing

    # Install crash handlers BEFORE we touch PJSIP -- a startup-time
    # native fault in libCreate is exactly the class of bug we most
    # need traces for. faulthandler + sys.excepthook + threading
    # excepthook all wired here; Sentry SDK opt-in via DSN env-var
    # or config_dir()/sentry.dsn.
    install_crash_handler()
    log.info("Starting %s", __app_name__)

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    QApplication.setApplicationName(__app_name__)
    QApplication.setOrganizationName(__app_name__)

    app = QApplication(argv)
    app.setWindowIcon(_load_icon())

    # Orphan-window detector. Logs one WARNING line per unique top-
    # level QWidget shown during the session so we can identify any
    # stray window that surfaces as its own NOC_Beam taskbar entry:
    #     [ORPHAN-WINDOW] <ClassName> title='<title>' parent=<None|...>
    # Force-on for the current debug rollout (was env-var-gated). Once
    # the orphan is identified and patched, drop this block or revert
    # to the env-var gate. Cost is one event filter on the Qt event
    # loop + a one-shot warning per top-level widget — not a per-frame
    # hit.
    if True:  # set to False to disable; was: os.environ.get("NOC_BEAM_DEBUG_ORPHANS") == "1"
        import os  # kept inside the block so the env-var gate is trivial to restore
        from PySide6.QtCore import QEvent, QObject
        from PySide6.QtWidgets import QWidget

        _seen_orphans: set[int] = set()

        class _OrphanWindowFilter(QObject):
            def eventFilter(self, obj, event):  # noqa: ANN001, N802
                try:
                    if (
                        event.type() == QEvent.Type.Show
                        and isinstance(obj, QWidget)
                        and obj.isWindow()
                        and id(obj) not in _seen_orphans
                    ):
                        _seen_orphans.add(id(obj))
                        parent = obj.parent()
                        # Extra identifiers so we can pinpoint the source
                        # of an orphan QLabel: objectName() catches anything
                        # that set one (rail/title-bar QSS selectors), and
                        # text() shows the label content for unstyled
                        # one-shot labels (e.g. "Welcome to NOC_Beam").
                        obj_name = ""
                        text_snippet = ""
                        try:
                            obj_name = obj.objectName() or ""
                        except Exception:
                            pass
                        try:
                            # Only QLabel/QPushButton/QToolButton/QLineEdit
                            # expose text(); guard with hasattr because
                            # PhoneShell etc. don't.
                            if hasattr(obj, "text"):
                                txt = obj.text()
                                if isinstance(txt, str):
                                    text_snippet = txt[:60]
                        except Exception:
                            pass
                        log.warning(
                            "[ORPHAN-WINDOW] %s objectName=%r text=%r title=%r "
                            "parent=%s flags=%s",
                            type(obj).__name__,
                            obj_name,
                            text_snippet,
                            obj.windowTitle(),
                            type(parent).__name__ if parent else "None",
                            int(obj.windowFlags()),
                        )
                        # Stack trace at Show-time pins the calling code.
                        # Cover the generic Qt classes that shouldn't
                        # normally be top-level (QLabel, plain QWidget,
                        # bare QFrame). Named subclasses like PhoneShell,
                        # TestRunnerView, QMenu, QComboBox popups are
                        # expected to be top-level so we skip those.
                        if type(obj).__name__ in ("QLabel", "QWidget", "QFrame"):
                            try:
                                import traceback
                                stack = traceback.format_stack(limit=15)
                                # Strip Qt event-loop frames; keep the
                                # last ~6 user frames before this filter.
                                user_frames = [
                                    f for f in stack
                                    if "noc_beam" in f
                                ][-6:]
                                if user_frames:
                                    log.warning(
                                        "[ORPHAN-WINDOW] stack:\n%s",
                                        "".join(user_frames),
                                    )
                            except Exception:
                                pass
                except Exception:
                    pass
                return False

        _orphan_filter = _OrphanWindowFilter()
        app.installEventFilter(_orphan_filter)
        log.info("Orphan-window detector active (NOC_BEAM_DEBUG_ORPHANS=1)")

    # Load persisted settings to pick the theme. PhoneShell loads them
    # again itself; this is the small price of theme being a process-
    # wide concern (QApplication.setStyleSheet) while the rest of
    # settings live on the window. Default theme is "dark" (the redesign
    # default the dataclass now ships); light / dark-hc remain available
    # for users who prefer them.
    settings = load_settings()
    theme = getattr(settings.appearance, "theme", "dark")
    apply_theme(app, settings.appearance.high_contrast, theme=theme)

    # Dark native title bars. QSS can't reach the native Windows chrome, so
    # in dark mode every window (main, Settings, Accounts, trace, runner...)
    # would sit under a glaring white DWM title bar. A single app-wide event
    # filter stamps DWMWA_USE_IMMERSIVE_DARK_MODE on each top-level widget at
    # Show time -- centralized here so no individual view needs to know about
    # DWM. The theme is re-read from persisted settings on every Show, so
    # dialogs opened after a runtime theme switch get the right chrome.
    # Known limitation: windows already on screen when the theme changes keep
    # their old bar color until re-shown/recreated.
    try:
        from noc_beam.ui.native_chrome import DarkTitleBarFilter

        def _current_theme() -> str:
            # Prefer the in-process theme cached on the QApplication by
            # apply_theme (updated at startup and on every runtime theme
            # switch -- the only ways the theme changes). Reading it here
            # avoids a synchronous settings.json read+parse on the GUI
            # thread for EVERY top-level Show: menus, combo popups and
            # tooltips all fire Show, so the previous per-Show load_settings()
            # added disk I/O to routine dropdown opens. Fall back to a disk
            # read, then to the startup theme, if the cache isn't set yet.
            cached = app.property("noc_active_theme")
            if cached:
                return str(cached)
            try:
                s = load_settings()
                return getattr(s.appearance, "theme", "dark")
            except Exception:
                return theme

        _dark_chrome_filter = DarkTitleBarFilter(_current_theme, parent=app)
        app.installEventFilter(_dark_chrome_filter)
    except Exception:
        # Cosmetic feature -- a white title bar is ugly, not fatal.
        log.exception("Could not install dark-title-bar filter")

    # FAS detection engine. The audio tap is wired per-call in
    # sip/call.py:onCallMediaState; this just spins up the worker
    # thread so it's ready when the first call confirms. Honours
    # FasSettings.enabled -- when False, attach_fas_to_call becomes
    # a no-op throughout the process lifetime.
    try:
        from noc_beam.audio.fas_engine import start_fas_engine

        fas_cfg = getattr(settings, "fas", None)
        start_fas_engine(enabled=bool(fas_cfg.enabled) if fas_cfg else True)
    except Exception:
        log.exception("FAS engine failed to start; continuing without FAS detection")

    window = PhoneShell()

    # Activation IPC for later launches. The Win32 mutex above already
    # refused the second instance; this server is how that refused instance
    # tells US (the first instance) to come to the front. Started only after
    # the window exists so the callback always has something to raise. The
    # QLocalServer delivers newConnection on this (GUI) thread, so the
    # callback -- which touches Qt widgets -- runs safely on the GUI thread.
    def _activate_main_window() -> None:
        try:
            # Reuse the exact tray-restore path so a window that was
            # minimized-to-tray (hidden) actually reappears, not just an
            # already-visible one. _restore_from_tray does
            # showNormal()+raise_()+activateWindow(); fall back to the same
            # calls directly if the method is ever renamed.
            restore = getattr(window, "_restore_from_tray", None)
            if callable(restore):
                restore()
            else:
                window.showNormal()
                window.raise_()
                window.activateWindow()
        except Exception:
            log.exception("Failed to activate main window on second-launch signal")

    try:
        from noc_beam.single_instance import ActivationServer

        # Kept on the app object so it lives for the process lifetime and is
        # torn down with the QApplication rather than garbage-collected early.
        app._activation_server = ActivationServer(_activate_main_window, parent=app)
    except Exception:
        # Activation is a convenience; if it can't start, the app still runs
        # (a second launch just won't raise us). Never fatal.
        log.exception("Could not start activation server; second launches won't raise the window")

    # Honour StartupSettings persisted from Settings -> General.
    # start_minimized launches into the tray (or minimized to taskbar
    # if no tray) instead of popping a foreground window. Was
    # display-only at the checkbox layer until this hook.
    _start_cfg = getattr(settings, "startup", None)
    if _start_cfg is not None and getattr(_start_cfg, "start_minimized", False):
        if getattr(window, "tray", None) is not None and window.tray.available:
            window.hide()
        else:
            window.showMinimized()
    else:
        window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(run(sys.argv))
