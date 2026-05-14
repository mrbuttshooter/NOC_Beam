"""Account wrapper around pjsua2.Account."""
from __future__ import annotations

import logging

from noc_beam.config.store import AccountConfig
from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE, pj
from noc_beam.sip.call import SipCall
from noc_beam.sip.events import sip_events

log = logging.getLogger(__name__)


def _transport_id_for(transport: str, transports: dict[str, int]) -> int:
    return transports.get(transport.lower(), transports.get("udp", -1))


def _srtp_use(setting: str) -> int:
    # PJSUA_SRTP_DISABLED=0, OPTIONAL=1, MANDATORY=2
    return {"disabled": 0, "optional": 1, "mandatory": 2}.get(setting.lower(), 0)


def _dtmf_method(setting: str) -> int:
    # PJSUA_DTMF_METHOD_RFC2833=0, SIP_INFO=1
    return {"rfc2833": 0, "info": 1, "inband": 0}.get(setting.lower(), 0)


if PJSUA2_AVAILABLE:

    class SipAccount(pj.Account):  # type: ignore[misc, name-defined]
        def __init__(self, cfg: AccountConfig, transports: dict[str, int]) -> None:
            super().__init__()
            self.cfg = cfg
            self._transports = transports
            self.calls: list[SipCall] = []

        # ------------------------------------------------------------------
        # pjsua2 callbacks
        # ------------------------------------------------------------------
        def onRegState(self, prm) -> None:  # noqa: N802, ANN001
            try:
                info = self.getInfo()
                sip_events().registration_changed.emit(
                    self.cfg.id, info.regStatus, info.regStatusText
                )
            except Exception:
                log.exception("onRegState error")

        def onIncomingCall(self, prm) -> None:  # noqa: N802, ANN001
            try:
                call = SipCall(self, prm.callId, self.cfg.id)
                self.calls.append(call)
                info = call.getInfo()
                sip_events().call_incoming.emit(
                    self.cfg.id, info.id, info.remoteUri, True
                )
            except Exception:
                log.exception("onIncomingCall error")

        # ------------------------------------------------------------------
        # Public helpers
        # ------------------------------------------------------------------
        def configure(self) -> "pj.AccountConfig":  # type: ignore[name-defined]
            ac = pj.AccountConfig()
            cfg = self.cfg

            ac.idUri = f"sip:{cfg.username}@{cfg.domain}"
            registrar = cfg.domain
            ac.regConfig.registrarUri = f"sip:{registrar}"
            ac.regConfig.registerOnAdd = cfg.register

            cred = pj.AuthCredInfo("digest", "*", cfg.auth_user or cfg.username, 0, cfg.password)
            ac.sipConfig.authCreds.append(cred)

            if cfg.proxy:
                ac.sipConfig.proxies.append(cfg.proxy)

            tid = _transport_id_for(cfg.transport, self._transports)
            if tid >= 0:
                ac.sipConfig.transportId = tid

            ac.mediaConfig.srtpUse = _srtp_use(cfg.srtp)
            ac.mediaConfig.srtpSecureSignaling = 0 if cfg.srtp != "mandatory" else 1

            if cfg.stun_server:
                ac.natConfig.sipStunUse = 1
                ac.natConfig.mediaStunUse = 1
            return ac

else:

    class SipAccount:  # type: ignore[no-redef]
        def __init__(self, *a, **kw) -> None:
            raise RuntimeError("pjsua2 not available")
