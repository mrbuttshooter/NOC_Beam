"""In-call quality sampler.

Polls pjsua2's stream stats while a call is CONFIRMED and emits a
`call_quality` signal with (mos, packet_loss%, jitter_ms, rtt_ms).
MOS is derived from the E-model R-factor (ITU-T G.107) using a coarse
voice-codec impairment baseline; we are not a compliance tool but the
4-bar bucket in the UI maps cleanly to perceived quality.

The sampler is intentionally cheap (one timer per active call) and pure
Python — no PJSIP-side configuration changes needed.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer

from noc_beam.sip.call_manager import CallManager, CallState
from noc_beam.sip.events import sip_events

log = logging.getLogger(__name__)

POLL_MS = 2000


def r_factor_to_mos(r: float) -> float:
    """ITU-T G.107 R-factor → MOS conversion."""
    if r <= 0:
        return 1.0
    if r >= 100:
        return 4.5
    mos = 1.0 + 0.035 * r + 7e-6 * r * (r - 60) * (100 - r)
    return max(1.0, min(4.5, mos))


def estimate_mos(packet_loss_pct: float, jitter_ms: float, rtt_ms: float) -> float:
    """Quick-and-decent MOS estimate without payload-type telemetry.

    R = R0 - Id - Ie_eff
      R0       baseline 93.2 for narrowband voice
      Id       delay impairment ≈ 0.024·D + 0.11·(D - 177.3)·H(D-177.3)
      Ie_eff   equipment impairment dominated by packet loss; rough
               approximation 30·loss% for the codec mix we expect.
    Jitter feeds into the effective delay as a small constant.
    """
    one_way = (rtt_ms / 2.0) + jitter_ms
    Id = 0.024 * one_way
    if one_way > 177.3:
        Id += 0.11 * (one_way - 177.3)
    Ie_eff = min(95.0, 30.0 * packet_loss_pct)
    R = 93.2 - Id - Ie_eff
    return r_factor_to_mos(R)


class CallQualitySampler(QObject):
    """Single QTimer that polls every active call's RTCP stats."""

    def __init__(self, manager: CallManager, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        # Start lazily -- the timer fires forever otherwise, burning
        # CPU + battery during idle. Hooked to CallManager so we
        # auto-arm whenever a call is added or its state changes
        # toward CONFIRMED.
        self._started = False
        # Store the lambda references so destroyed-time teardown can
        # disconnect them by slot. Without this each PhoneShell
        # instance leaked a pair of lambdas onto the singleton
        # call_manager (same class of bug as the strip-refresh
        # lambdas the v3 audit identified as the test-hang root cause).
        self._arm_on_added = lambda _cid: self._ensure_running()
        self._arm_on_updated = lambda _cid: self._ensure_running()
        try:
            manager.call_added.connect(self._arm_on_added)
            manager.call_updated.connect(self._arm_on_updated)
        except Exception:
            pass
        self.destroyed.connect(self._disconnect)

    def _disconnect(self, *_args) -> None:
        try:
            self._manager.call_added.disconnect(self._arm_on_added)
        except Exception:
            pass
        try:
            self._manager.call_updated.disconnect(self._arm_on_updated)
        except Exception:
            pass
        try:
            self._timer.stop()
        except Exception:
            pass

    def _ensure_running(self) -> None:
        if not self._started:
            self._timer.start()
            self._started = True

    def _maybe_pause(self) -> None:
        """Stop polling if there's nothing to sample (no active calls)."""
        if not any(
            r.state in (CallState.CONFIRMED, CallState.HELD)
            for r in self._manager.all()
        ):
            self._timer.stop()
            self._started = False

    def _poll(self) -> None:
        active = [r for r in self._manager.all()
                  if r.state in (CallState.CONFIRMED, CallState.HELD)]
        if not active:
            # No live calls -- pause the timer until next call ramps up
            # (CallManager call_added/updated will re-arm via _ensure_running).
            self._timer.stop()
            self._started = False
            return
        # Late import — endpoint may not exist yet when this object is built.
        from noc_beam.sip.endpoint import SipEndpoint

        ep = SipEndpoint.instance()
        for rec in active:
            call = ep.find_call(rec.call_id)
            if call is None:
                continue
            try:
                stats = self._read_stats(call)
            except Exception:
                log.exception("RTCP read failed for call %s", rec.call_id)
                continue
            if stats is None:
                continue
            loss, jitter_ms, rtt_ms = stats
            mos = estimate_mos(loss, jitter_ms, rtt_ms)
            sip_events().call_quality.emit(rec.call_id, mos, loss, jitter_ms, rtt_ms)

    @staticmethod
    def _read_stats(call) -> tuple[float, float, float] | None:  # noqa: ANN001
        """Pull (loss_pct, jitter_ms, rtt_ms) out of pjsua2's StreamStat.

        pjsua2's API is shaped roughly:
            si = call.getStreamInfo(idx)
            ss = call.getStreamStat(idx)
        with `ss.rtcp.rxStat.loss` / `txStat`, `ss.rtcp.rttUsec`, etc.
        We pick the first active audio stream and tolerate API drift.
        """
        info = call.getInfo()
        for mi in info.media:
            if mi.type != 1 or mi.status != 1:    # audio + active
                continue
            try:
                ss = call.getStreamStat(mi.index)
            except Exception:
                return None

            rtcp = getattr(ss, "rtcp", None)
            if rtcp is None:
                return None

            # Loss: pjsua2 exposes rxStat.loss (packet count) and rxStat.pkt.
            rx = getattr(rtcp, "rxStat", None)
            if rx is None:
                return None
            loss_pkts = float(getattr(rx, "loss", 0))
            received = float(getattr(rx, "pkt", 0))
            total_pkts = received + loss_pkts
            # When PJSIP reports loss > 0 with received == 0 (burst-
            # loss start, or stream just primed), total_pkts == loss
            # would yield 100% loss every poll until the first packet
            # actually lands. Treat that case as "unknown" (0%) until
            # at least one packet arrives, so MOS doesn't fall off
            # a cliff on every call's first RTCP tick.
            if total_pkts > 0 and received > 0:
                loss_pct = 100.0 * loss_pkts / total_pkts
            else:
                loss_pct = 0.0

            jitter_us = float(getattr(getattr(rx, "jitterUsec", None), "mean", 0) or 0)
            jitter_ms = jitter_us / 1000.0

            rtt_us_obj = getattr(rtcp, "rttUsec", None)
            rtt_us = float(getattr(rtt_us_obj, "mean", 0) or 0) if rtt_us_obj else 0.0
            rtt_ms = rtt_us / 1000.0

            return loss_pct, jitter_ms, rtt_ms
        return None
