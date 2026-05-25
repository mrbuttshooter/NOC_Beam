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
        kernel32 = ctypes.windll.kernel32
        # bInitialOwner=True so the first instance immediately owns it.
        # The handle is intentionally leaked to module scope; process
        # exit releases it. Name is in the Global\ namespace so it works
        # across user sessions on the same machine (terminal services /
        # fast user switching) -- last-writer-wins on accounts.json is a
        # machine-wide concern, not a per-user one.
        _SINGLE_INSTANCE_MUTEX = kernel32.CreateMutexW(None, True, _SINGLE_INSTANCE_NAME)
        err = kernel32.GetLastError()
    except Exception:
        log.exception("Single-instance check failed; allowing startup")
        return None

    if err != _ERROR_ALREADY_EXISTS:
        return None

    # Another instance is already running. Show a friendly dialog and
    # bail with exit code 0 (this is the expected user-facing outcome,
    # not a failure).
    log.warning("NOC_Beam is already running; aborting second instance")
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        _msg_app = QApplication.instance() or QApplication(argv)
        QMessageBox.information(
            None,
            __app_name__,
            "NOC_Beam is already running. Check your system tray.",
        )
    except Exception:
        # If even the message box fails (no display, etc.), the log line
        # above is our breadcrumb. Still exit cleanly.
        log.exception("Failed to show already-running message box")
    return 0


def run(argv: list[str]) -> int:
    # Single-instance guard FIRST -- before logging setup, crash handler,
    # or any PJSIP/Qt construction. Two NOC_Beam processes both writing
    # accounts.json + call_history.json silently lose CDRs (last-writer-
    # wins), and PJSIP itself wants a singleton process.
    _existing = _acquire_single_instance_or_exit(argv)
    if _existing is not None:
        return _existing

    setup_logging()
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
    # settings live on the window. Default theme is "light" (the
    # Bria-evolution direction); dark / dark-hc remain available for
    # users who prefer the original NOC dashboard look.
    settings = load_settings()
    theme = getattr(settings.appearance, "theme", "light")
    apply_theme(app, settings.appearance.high_contrast, theme=theme)

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
