"""Crash + unhandled-exception capture for NOC_Beam.

Two failure modes need different machinery:

  1. Python-level unhandled exceptions  -> sys.excepthook + threading.excepthook
     Logs the traceback, writes a structured crash record to
     %APPDATA%/NOC_Beam/crashes/python-YYYYMMDD-HHMMSS.log, and (if
     a Sentry DSN is configured) ships it to Sentry.

  2. Native-side crashes (PJSIP / SWIG)  -> faulthandler.enable()
     Faulthandler writes a C-level traceback of the crashing thread
     to a pre-opened file BEFORE Python tears down -- the only way
     to see anything when pjmedia null-derefs or a callback re-enters
     a destroyed Endpoint. The file lives next to the python-* crash
     records so a "Send diagnostics" bundle can grab everything.

Sentry integration is OPT-IN. We never send telemetry without a DSN
in either:
  - SENTRY_DSN environment variable
  - config_dir() / "sentry.dsn" (single-line text file the user can
    drop in if their org runs a Sentry instance)
No DSN means file-only crash capture, which is still strictly better
than the zero-telemetry baseline we had.
"""
from __future__ import annotations

import faulthandler
import itertools
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

log = logging.getLogger(__name__)

_INSTALLED = False
_FH_FILE = None  # keep the faulthandler file alive for the process lifetime
_SENTRY_INITIALIZED = False

# Monotonic counter so two crash records written within the same wall-clock
# second get distinct filenames. Without it, an exception loop (or two
# threads faulting back-to-back) formatted the same "%Y%m%d-%H%M%S" name and
# each write clobbered the previous crash record -- losing all but the last.
_crash_seq = itertools.count()


def _crash_dir() -> Path:
    """Return %APPDATA%/NOC_Beam/crashes (created on demand)."""
    from noc_beam.config.paths import data_dir

    out = data_dir() / "crashes"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_crash_record(kind: str, header: str, body: str) -> Path:
    """Persist a structured crash to crashes/<kind>-<ts>.log. Returns
    the path so the host can offer it via "Send diagnostics"."""
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    # Base name carries pid + a monotonic sequence so records written in
    # the same second never share a name. Then use exclusive-create ("x")
    # and bump a suffix if the (astronomically unlikely) collision still
    # happens -- guarantees we never truncate a prior crash record.
    pid = os.getpid()
    seq = next(_crash_seq)
    body_text = f"{header}\n\n{body}\n"
    path = _crash_dir() / f"{kind}-{ts}-{pid}-{seq}.log"
    for attempt in range(100):
        candidate = (
            path
            if attempt == 0
            else _crash_dir() / f"{kind}-{ts}-{pid}-{seq}-{attempt}.log"
        )
        try:
            # "x" fails if the file already exists instead of truncating it.
            with open(candidate, "x", encoding="utf-8") as f:
                f.write(body_text)
            return candidate
        except FileExistsError:
            continue
        except Exception:
            log.exception("Failed to write crash record (%s)", candidate)
            return candidate
    log.error("Could not find a free crash-record filename for %s", path)
    return path


def _maybe_init_sentry() -> None:
    """Best-effort Sentry SDK init. Silently skipped when no DSN or
    sdk isn't installed in this build. Never raises."""
    global _SENTRY_INITIALIZED
    if _SENTRY_INITIALIZED:
        return
    import os

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        try:
            from noc_beam.config.paths import config_dir

            dsn_file = config_dir() / "sentry.dsn"
            if dsn_file.exists():
                dsn = dsn_file.read_text(encoding="utf-8").strip()
        except Exception:
            dsn = ""
    if not dsn:
        log.debug("No Sentry DSN configured; crash uploads disabled")
        return
    try:
        import sentry_sdk  # type: ignore[import-not-found]

        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.0,
            send_default_pii=False,
            attach_stacktrace=True,
        )
        _SENTRY_INITIALIZED = True
        log.info("Sentry crash uploads enabled")
    except ImportError:
        log.debug("sentry_sdk not installed; crash uploads to file only")
    except Exception:
        log.exception("Sentry init failed; crash uploads disabled")


def _python_excepthook(exc_type, exc, tb) -> None:
    """Replacement for sys.excepthook. Logs to root logger AND writes
    a crash record to disk. Falls through to the system default after
    so any embedded debugger / IDE still sees the exception."""
    # Don't intercept KeyboardInterrupt -- ctrl-c should just exit.
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    try:
        body = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("UNHANDLED EXCEPTION on %s:\n%s",
                  threading.current_thread().name, body)
        _write_crash_record(
            "python",
            header=f"Unhandled {exc_type.__name__} on "
                   f"{threading.current_thread().name}",
            body=body,
        )
        if _SENTRY_INITIALIZED:
            try:
                import sentry_sdk  # type: ignore[import-not-found]
                sentry_sdk.capture_exception((exc_type, exc, tb))
            except Exception:
                pass
    except Exception:
        # Last-ditch: never let the crash hook itself crash silently.
        # Chain to the original here and RETURN so we don't fall through
        # to the unconditional call below -- otherwise a failure in the
        # hook body printed the traceback twice (once from this except
        # path, once from the tail call).
        sys.__excepthook__(exc_type, exc, tb)
        return
    # Chain to the original so the process exit / IDE notification path
    # still runs. Reached only when the hook body above succeeded, so
    # __excepthook__ runs exactly once on every path.
    sys.__excepthook__(exc_type, exc, tb)


def _thread_excepthook(args) -> None:  # type: ignore[no-untyped-def]
    """Same as _python_excepthook but for threading.excepthook -- bare
    threads (PJSIP workers, our QTimer-spawned helpers) don't go through
    sys.excepthook by default in Python 3.8+."""
    if args.exc_type is None:
        return
    try:
        _python_excepthook(args.exc_type, args.exc_value, args.exc_traceback)
    except Exception:
        # Restore default so we don't swallow the whole report.
        threading.__excepthook__(args)  # type: ignore[attr-defined]


def install() -> None:
    """Wire up the three capture paths. Idempotent."""
    global _INSTALLED, _FH_FILE
    if _INSTALLED:
        return
    _INSTALLED = True
    try:
        sys.excepthook = _python_excepthook
        threading.excepthook = _thread_excepthook
    except Exception:
        log.exception("Could not install Python exception hooks")
    try:
        fh_path = _crash_dir() / "native-current.log"
        # `w` truncates per run -- prior native crash files are
        # rotated via rename below so we don't lose them.
        prior = _crash_dir() / "native-previous.log"
        rotated = True
        if fh_path.exists():
            try:
                if prior.exists():
                    prior.unlink()
                fh_path.rename(prior)
            except Exception:
                # Rotation failed (AV lock, permissions). Do NOT fall
                # through to open(..., "w") -- that would truncate the ONLY
                # record of the previous run's native crash. Append instead
                # so the prior evidence is preserved (faulthandler just
                # writes its next traceback after the old one).
                rotated = False
                log.warning(
                    "Could not rotate %s; appending to preserve prior native crash",
                    fh_path.name,
                    exc_info=True,
                )
        _FH_FILE = open(fh_path, "w" if rotated else "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(_FH_FILE)
        log.info("faulthandler enabled -> %s", fh_path)
    except Exception:
        log.exception("Could not enable faulthandler")
    try:
        _maybe_init_sentry()
    except Exception:
        log.exception("Sentry init dispatch failed")
