"""Per-account registration retry with exponential backoff.

PJSIP retries some failure modes internally (transport, DNS) but does not
back off cleanly on hard failures like 401 unauthorised or 408 timeout —
the registrar can be pounded if we don't gate it. This controller listens
to `registration_changed` events and schedules a re-REGISTER with backoff:
1 s → 2 s → 4 s → 8 s → 16 s → 30 s (capped). Resets to 1 s on a 2xx.

Auth failures (401/403/407) are *not* retried — those mean the credentials
are wrong and retrying will just lock the account out. Other 4xx/5xx and
408/503/504 are retried.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer

from noc_beam.sip.events import sip_events

log = logging.getLogger(__name__)


# 2xx (success) clears the schedule; anything else (within the retryable
# set below) triggers backoff. Adjust this list rather than the logic.
#
# 401/403/407 are auth rejections; retrying just locks the account out.
# 405 means this trunk does not accept REGISTER at all (common for
# IP-auth origination trunks); retrying only adds noise while INVITE
# may still be allowed.
# 423 ("Interval Too Brief") is NOT in this set: it means the registrar
# wants a longer Expires (per its Min-Expires response header), and the
# correct response is to re-REGISTER with that value. We don't have access
# to the Min-Expires header at this signal layer (pjsua2 doesn't surface
# it on registration_changed), so 423 falls through to the normal
# retry-with-backoff path; PJSIP's own resolver may pick up the
# Min-Expires on the retry, and at minimum we keep trying instead of
# silently giving up.
_NO_RETRY_CODES = {401, 403, 405, 407}     # don't retry hard rejections
_RETRY_INTERVALS_MS = [1000, 2000, 4000, 8000, 16000, 30000]

# After this many sustained retries at the 30 s cap (≈30 min of continuous
# failure) the chain switches to a long-sleep interval. A field log from a
# user's machine showed a single 503-pinned account racking up 1180+
# attempts in 9 hours at 30 s each — pointless network noise and a fast
# track to carrier rate-limit bans. We keep retrying forever (carriers
# sometimes recover after hours) but slow the cadence drastically.
MAX_FAST_RETRIES = 60
LONG_SLEEP_INTERVAL_MS = 15 * 60 * 1000   # 15 minutes


class RegistrationRetry(QObject):
    """Owns one timer per account_id; reads endpoint from the singleton."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._attempts: dict[str, int] = {}
        self._timers: dict[str, QTimer] = {}
        # Connect to the singleton sip_events. Disconnect on
        # destruction so we don't keep firing into a dead RegistrationRetry
        # if PhoneShell is closed and re-opened (or in tests).
        sip_events().registration_changed.connect(self._on_registration_changed)
        self.destroyed.connect(self._on_destroyed)

    def _on_destroyed(self, *_args) -> None:
        try:
            sip_events().registration_changed.disconnect(self._on_registration_changed)
        except Exception:
            pass
        # Stop all pending timers so they don't fire into self after
        # Qt has destroyed the C++ side.
        for t in list(self._timers.values()):
            try:
                t.stop()
            except Exception:
                pass
        self._timers.clear()

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------
    def _on_registration_changed(self, account_id: str, code: int, reason: str) -> None:
        if code == 0:
            return  # initial/no-op transitions
        if 200 <= code < 300:
            self._reset(account_id)
            return
        if code in _NO_RETRY_CODES:
            log.info("Account %s rejected (%d %s) — not retrying", account_id, code, reason)
            self._reset(account_id)
            return
        # Retry-eligible failure.
        self._schedule_retry(account_id, code, reason)

    def _schedule_retry(self, account_id: str, code: int, reason: str) -> None:
        attempt = self._attempts.get(account_id, 0)
        # After MAX_FAST_RETRIES sustained failures, switch to long-sleep
        # mode: keep retrying forever but at LONG_SLEEP_INTERVAL_MS rather
        # than the 30 s cap. The attempt counter keeps incrementing; only
        # the interval changes. Log the transition exactly once (on the
        # first scheduling that uses the long-sleep interval).
        if attempt >= MAX_FAST_RETRIES:
            interval = LONG_SLEEP_INTERVAL_MS
            if attempt == MAX_FAST_RETRIES:
                log.warning(
                    "Account %s reached MAX_FAST_RETRIES=%d; switching to "
                    "long-sleep retry (%d min interval)",
                    account_id, MAX_FAST_RETRIES, LONG_SLEEP_INTERVAL_MS // 60000,
                )
        else:
            interval = _RETRY_INTERVALS_MS[min(attempt, len(_RETRY_INTERVALS_MS) - 1)]
        self._attempts[account_id] = attempt + 1
        log.warning(
            "Registration failure for %s (%d %s); retry in %d ms (attempt %d)",
            account_id, code, reason, interval, attempt + 1,
        )

        old_timer = self._timers.pop(account_id, None)
        if old_timer is not None:
            old_timer.stop()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda aid=account_id: self._do_retry(aid))
        timer.start(interval)
        self._timers[account_id] = timer

    def _do_retry(self, account_id: str) -> None:
        # Late import to avoid the SIP endpoint being constructed by this
        # module's import (the controller is built in main_window before
        # the endpoint exists).
        from noc_beam.sip.endpoint import SipEndpoint

        ep = SipEndpoint.instance()
        acc = ep.get_account(account_id)
        if acc is None:
            log.info("Retry skipped — account %s no longer registered", account_id)
            return
        try:
            # pjsua2: account.setRegistration(True) issues a fresh REGISTER.
            acc.setRegistration(True)
        except Exception as exc:
            # Generic pjsua2.Error here is expected and transient: the
            # account handle is mid-state-transition (another REGISTER
            # already in flight, transport rebinding, etc.). Log at
            # WARN (not ERROR) and re-schedule another retry with the
            # next backoff step so the chain keeps trying instead of
            # silently dying on one transient race.
            log.warning(
                "setRegistration race on retry for %s (%s); rescheduling",
                account_id, type(exc).__name__,
            )
            # Re-schedule via the normal backoff path so the chain
            # keeps trying instead of silently dying on one transient
            # race. _schedule_retry uses _attempts[account_id] as the
            # backoff index, so the wait grows naturally with repeated
            # races.
            self._schedule_retry(account_id, 0, "transient")

    def _reset(self, account_id: str) -> None:
        self._attempts.pop(account_id, None)
        timer = self._timers.pop(account_id, None)
        if timer is not None:
            timer.stop()

    def reset(self, account_id: str) -> None:
        """Public clear -- call this from the host when an account is
        removed so a stale retry doesn't fire after removal."""
        self._reset(account_id)
