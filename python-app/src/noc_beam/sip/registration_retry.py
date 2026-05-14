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
_AUTH_REJECT_CODES = {401, 403, 407, 423}     # don't retry — credential issue
_RETRY_INTERVALS_MS = [1000, 2000, 4000, 8000, 16000, 30000]


class RegistrationRetry(QObject):
    """Owns one timer per account_id; reads endpoint from the singleton."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._attempts: dict[str, int] = {}
        self._timers: dict[str, QTimer] = {}
        sip_events().registration_changed.connect(self._on_registration_changed)

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------
    def _on_registration_changed(self, account_id: str, code: int, reason: str) -> None:
        if code == 0:
            return  # initial/no-op transitions
        if 200 <= code < 300:
            self._reset(account_id)
            return
        if code in _AUTH_REJECT_CODES:
            log.info("Account %s rejected (%d %s) — not retrying", account_id, code, reason)
            self._reset(account_id)
            return
        # Retry-eligible failure.
        self._schedule_retry(account_id, code, reason)

    def _schedule_retry(self, account_id: str, code: int, reason: str) -> None:
        attempt = self._attempts.get(account_id, 0)
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
        except Exception:
            log.exception("setRegistration on retry failed for %s", account_id)

    def _reset(self, account_id: str) -> None:
        self._attempts.pop(account_id, None)
        timer = self._timers.pop(account_id, None)
        if timer is not None:
            timer.stop()
