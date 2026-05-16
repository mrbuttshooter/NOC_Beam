"""Call wrapper around pjsua2.Call.

All pjsua2 callbacks happen on PJSIP threads. Each callback acquires the
library lock automatically (pjsua2 docs), but we must NOT call into Qt from
them — we emit on SipEvents and let Qt deliver on the main thread.
"""
from __future__ import annotations

import logging

from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE, pj
from noc_beam.sip.events import sip_events

log = logging.getLogger(__name__)


_STATE_NAMES = {
    0: "NULL",
    1: "CALLING",
    2: "INCOMING",
    3: "EARLY",
    4: "CONNECTING",
    5: "CONFIRMED",
    6: "DISCONNECTED",
}


if PJSUA2_AVAILABLE:

    class SipCall(pj.Call):  # type: ignore[misc, name-defined]
        """A single dialog. Lifecycle ends at DISCONNECTED."""

        def __init__(self, account, call_id: int = -1, account_id: str = "") -> None:  # noqa: ANN001
            # Stash account ref BEFORE super().__init__ so PJSIP
            # callbacks (onCallState etc.) that may fire on a worker
            # thread during the C++ super-init have access to it.
            # Previously the assignment ran AFTER super().__init__,
            # so a fast onCallState (e.g. an incoming INVITE that's
            # already disconnected by the time we wrap it) could see
            # getattr(self, "_account", None) == None and skip the
            # "remove self from acc.calls" cleanup branch.
            self._account = account
            self._account_id = account_id
            self.remote_uri = ""
            super().__init__(account, call_id)

        # ------------------------------------------------------------------
        # pjsua2 callbacks (PJSIP thread)
        # ------------------------------------------------------------------
        def onCallState(self, prm) -> None:  # noqa: N802, ANN001
            try:
                info = self.getInfo()
                state_name = _STATE_NAMES.get(info.state, str(info.state))
                self.remote_uri = info.remoteUri
                sip_events().call_state_changed.emit(
                    self._account_id,
                    info.id,
                    state_name,
                    info.lastStatusCode,
                    info.lastReason,
                )
                if info.state == 6:  # PJSIP_INV_STATE_DISCONNECTED
                    sip_events().call_ended.emit(info.id)
                    # Drop from the account's calls list so find_call
                    # doesn't return this stale instance when PJSIP
                    # later reuses the same internal call-id slot
                    # for a new call. Without this, mute / hangup /
                    # quality-sample can act on a destroyed SipCall
                    # whose media indices are stale -- subtle race
                    # in long-running test sessions.
                    try:
                        acc = getattr(self, "_account", None)
                        if acc is not None and self in acc.calls:
                            acc.calls.remove(self)
                    except Exception:
                        log.exception("Could not remove disconnected SipCall from acc.calls")
            except Exception:
                log.exception("onCallState error")

        def onCallMediaState(self, prm) -> None:  # noqa: N802, ANN001
            try:
                info = self.getInfo()
                for mi in info.media:
                    # type 1 == audio, status 1 == active
                    if mi.type == 1 and mi.status == 1:
                        aud = self.getAudioMedia(mi.index)
                        # Hook into the default audio device manager
                        ep = pj.Endpoint.instance()
                        dev_mgr = ep.audDevManager()
                        dev_mgr.getCaptureDevMedia().startTransmit(aud)
                        aud.startTransmit(dev_mgr.getPlaybackDevMedia())

                        codec = ""
                        clock = 0
                        chans = 0
                        try:
                            # Best-effort codec readout via call's media stats
                            stat = self.getStreamInfo(mi.index)
                            codec = stat.codecName
                            clock = stat.codecClockRate
                            chans = getattr(stat, "audChannelCount", 1)
                        except Exception:
                            pass
                        sip_events().call_media_active.emit(info.id, codec, clock, chans)
            except Exception:
                log.exception("onCallMediaState error")

        def onDtmfDigit(self, prm) -> None:  # noqa: N802, ANN001
            try:
                info = self.getInfo()
                sip_events().call_dtmf.emit(info.id, prm.digit)
            except Exception:
                log.exception("onDtmfDigit error")

else:

    class SipCall:  # type: ignore[no-redef]
        def __init__(self, *a, **kw) -> None:
            raise RuntimeError("pjsua2 not available")
