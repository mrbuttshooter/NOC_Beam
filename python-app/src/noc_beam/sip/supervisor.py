"""PJSIP endpoint supervisor.

The SipEndpoint can lose its event-loop thread or get into a "I keep
emitting endpoint_error but no calls work" state -- common triggers:
  * transport collapse after a long suspend/resume
  * codec plugin abort on a specific media flow
  * callback re-entry against a torn-down account

We watch sip_events.endpoint_error. If we see N errors within a
short window AND there are no live calls, perform a controlled
restart: stop() the endpoint, re-add all configured accounts, and
let the existing RegistrationRetry / health surfaces pick it up.

The restart preserves CallManager state and PhoneShell's UI; we
only touch the SIP side. We refuse to restart while a call is
CONFIRMED -- the user is talking, do not disturb. Instead we
defer the restart to the next call-removed transition.
"""
from __future__ import annotations

import logging
import time
from collections import deque

from PySide6.QtCore import QObject, QTimer

from noc_beam.sip.events import sip_events

log = logging.getLogger(__name__)

# Trigger restart when we see this many endpoint_error events within
# the rolling window. Tuned to ignore single transient errors but
# react to "the endpoint is wedged" cascades.
_ERROR_THRESHOLD = 4
_WINDOW_SECONDS = 30.0
# Don't restart more often than this. Otherwise a misconfigured account
# could trap us in a restart loop.
_RESTART_COOLDOWN_SECONDS = 60.0


class EndpointSupervisor(QObject):
    """Hooks sip_events.endpoint_error and orchestrates a restart
    of SipEndpoint when the error rate crosses _ERROR_THRESHOLD."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._error_times: deque[float] = deque(maxlen=_ERROR_THRESHOLD)
        self._last_restart_at: float = 0.0
        self._deferred_pending: bool = False
        # Guards against double-scheduling a restart: two hangups inside
        # the 500ms defer window would otherwise each queue their own
        # QTimer.singleShot(500, self._do_restart), firing the restart
        # twice back-to-back (the second one tearing down the endpoint the
        # first one just rebuilt).
        self._restart_scheduled: bool = False
        sip_events().endpoint_error.connect(self._on_endpoint_error)
        # On call_ended, see if a deferred restart is queued.
        sip_events().call_ended.connect(self._on_call_ended)
        # Disconnect on destruction so the singleton signal mesh
        # doesn't keep firing into a dead supervisor.
        self.destroyed.connect(self._disconnect)

    def _disconnect(self, *_args) -> None:
        for sig, slot in (
            (sip_events().endpoint_error, self._on_endpoint_error),
            (sip_events().call_ended, self._on_call_ended),
        ):
            try:
                sig.disconnect(slot)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _on_endpoint_error(self, msg: str) -> None:
        """Called for every endpoint_error emission. Decide whether
        the rate justifies a restart."""
        now = time.monotonic()
        self._error_times.append(now)
        recent = [t for t in self._error_times if now - t <= _WINDOW_SECONDS]
        if len(recent) < _ERROR_THRESHOLD:
            return
        if now - self._last_restart_at < _RESTART_COOLDOWN_SECONDS:
            log.warning(
                "Endpoint error storm detected (%d in %.0fs) but cooldown "
                "is active; skipping restart", len(recent), _WINDOW_SECONDS,
            )
            return
        self._attempt_restart()

    def _on_call_ended(self, *_args) -> None:
        # Only schedule if a restart is deferred AND we haven't already
        # queued one. Without the _restart_scheduled guard, two hangups
        # inside the 500ms window each queue a singleShot(500, _do_restart)
        # and the endpoint gets torn down twice in a row.
        if self._deferred_pending and not self._restart_scheduled:
            log.info("Call ended; performing deferred endpoint restart")
            self._restart_scheduled = True
            QTimer.singleShot(500, self._do_restart)

    # ------------------------------------------------------------------
    def _attempt_restart(self) -> None:
        """Decide whether to restart now or defer to next call-ended."""
        try:
            from noc_beam.sip.call_manager import call_manager, CallState
            mgr = call_manager()
            if any(r.state in (CallState.CONFIRMED, CallState.HELD)
                   for r in mgr.all()):
                log.warning(
                    "Endpoint error storm detected but a call is in "
                    "progress; deferring restart until call ends."
                )
                self._deferred_pending = True
                return
        except Exception:
            log.exception("call_manager state check failed")
        self._do_restart()

    def _do_restart(self) -> None:
        """Controlled SIP-only restart. UI / CallManager are left alone."""
        from noc_beam.sip.endpoint import SipEndpoint

        # This singleShot has now fired; allow the next call-ended to
        # re-arm a fresh defer if we bail out below.
        self._restart_scheduled = False
        # Re-check for live calls BEFORE stopping the endpoint. The
        # confirmed/held check in _attempt_restart ran when the storm was
        # detected, but a call ending is not the same as ALL calls ending:
        # _on_call_ended fires per-call, so the call that just ended may
        # have left OTHER lines still CONFIRMED/HELD. stop()ing here would
        # kill those live calls. If any remain, stay deferred and wait for
        # the next call-ended.
        try:
            from noc_beam.sip.call_manager import call_manager, CallState
            mgr = call_manager()
            if any(r.state in (CallState.CONFIRMED, CallState.HELD)
                   for r in mgr.all()):
                log.info(
                    "Deferred restart fired but a call is still live; "
                    "re-deferring until the last call ends."
                )
                # Keep _deferred_pending set so the next call-ended re-arms.
                return
        except Exception:
            log.exception("call_manager state check failed in _do_restart")

        self._deferred_pending = False
        self._error_times.clear()
        self._last_restart_at = time.monotonic()
        log.warning(
            "Endpoint supervisor: restarting SipEndpoint (error threshold "
            "%d / %.0fs hit)", _ERROR_THRESHOLD, _WINDOW_SECONDS,
        )
        ep = SipEndpoint.instance()
        # Snapshot the configured accounts before stop wipes them.
        try:
            account_cfgs = [acc.cfg for acc in ep.accounts()]
        except Exception:
            log.exception("Snapshot accounts failed; restart aborted")
            return
        try:
            ep.stop()
        except Exception:
            log.exception("Endpoint.stop during supervised restart failed")
        try:
            # Re-fetch the persisted settings so any in-flight update is
            # picked up. Falls back to defaults if disk read fails.
            try:
                from noc_beam.config.store import load_settings
                settings = load_settings()
            except Exception:
                from noc_beam.config.store import GlobalSettings
                settings = GlobalSettings()
            ep.start(settings, accounts=account_cfgs)
        except Exception:
            log.exception("Endpoint.start during supervised restart failed")
            return
        # Re-add each account; ep.start clears the dict so we own the
        # repopulate. Errors are swallowed per-account.
        for cfg in account_cfgs:
            if not getattr(cfg, "enabled", True):
                continue
            try:
                ep.add_account(cfg)
            except Exception:
                log.exception(
                    "Re-add account %s after supervised restart failed",
                    getattr(cfg, "id", "?"),
                )
        log.warning("Endpoint supervisor: restart complete")
