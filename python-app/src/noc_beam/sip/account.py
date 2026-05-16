"""Account wrapper around pjsua2.Account."""
from __future__ import annotations

import logging

from noc_beam.config.store import AccountConfig
from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE, pj
from noc_beam.sip.call import SipCall
from noc_beam.sip.events import sip_events

log = logging.getLogger(__name__)


def _transport_id_for(transport: str, transports: dict[str, int]) -> int:
    """Look up the PJSIP transport ID for a requested transport.

    Earlier this silently fell back to UDP whenever the requested
    transport wasn't present (e.g. TLS bind failed at endpoint init).
    A TLS-configured account would then register over UDP with no
    diagnostic -- the user sees "Registered" and reasonably believes
    their credentials are encrypted on the wire. Now return -1 on
    miss so the caller can surface a clear error instead of
    downgrading in silence.
    """
    key = (transport or "udp").lower()
    return transports.get(key, -1)


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
            # Set by configure() when the requested transport isn't
            # bound; consumed by SipEndpoint.add_account after the
            # lock is released so the diagnostic emission can't
            # re-enter the endpoint lock.
            self._pending_transport_error: str | None = None

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

            # Append :port when the user set a non-default port. Without
            # this the per-account port field in the dialog was decorative.
            port = int(getattr(cfg, "port", 0) or 0)
            host = cfg.domain
            if port and not (
                cfg.domain.endswith(f":{port}") or "]" in cfg.domain
            ):
                host = f"{cfg.domain}:{port}"

            # `;transport=` parameter on registrarUri / contact. Without
            # this PJSIP picks transport based on URI scheme + DNS NAPTR
            # which often defaults to UDP even when the account is set to
            # TLS, producing the classic "registers fine on UDP, calls
            # fail because Contact: advertises a TLS port we never bound"
            # gotcha. Be explicit.
            transport = (cfg.transport or "udp").lower()
            scheme = "sips" if transport == "tls" else "sip"
            transport_param = f";transport={transport}" if transport in ("tcp", "tls") else ""
            ac.idUri = f"{scheme}:{cfg.username}@{host}{transport_param}"
            ac.regConfig.registrarUri = f"{scheme}:{host}{transport_param}"
            ac.regConfig.registerOnAdd = cfg.register

            # Auth realm: pinning to the account's domain instead of the
            # `*` wildcard prevents the credential from being offered to
            # an unrelated proxy that happens to share the same dialog
            # (the wildcard credential is a known information-leak vector
            # against malicious 401 challenges from off-path attackers).
            # Lowercased: Kamailio (default config) and some OpenSIPS
            # builds challenge with lowercase realm, AuthCredInfo match
            # is case-sensitive in older PJSIP -> 401 loop with correct
            # creds. Fall back to `*` only when the domain is blank.
            realm = (cfg.domain or "*").split(":", 1)[0].lower() or "*"
            cred = pj.AuthCredInfo(
                "digest", realm, cfg.auth_user or cfg.username, 0, cfg.password
            )
            ac.sipConfig.authCreds.append(cred)

            if cfg.proxy:
                ac.sipConfig.proxies.append(cfg.proxy)

            tid = _transport_id_for(cfg.transport, self._transports)
            if tid >= 0:
                ac.sipConfig.transportId = tid
            else:
                # Requested transport isn't bound. Don't silently fall
                # back to UDP -- log + flag for deferred diagnostic
                # emission. Earlier this fired sip_events().emit()
                # SYNCHRONOUSLY inside configure() while endpoint._lock
                # was held by add_account(); any subscriber that called
                # back into SipEndpoint would re-enter under the lock
                # and could deadlock or observe a half-built account.
                # The flag is read by SipEndpoint.add_account AFTER the
                # account is fully created and the lock released, then
                # the diagnostic is dispatched via QTimer.singleShot(0).
                log.error(
                    "Account %s requested transport=%s but no such "
                    "transport is bound; refusing silent UDP downgrade",
                    cfg.id, cfg.transport,
                )
                self._pending_transport_error = (
                    f"Transport '{cfg.transport}' unavailable; "
                    f"account will not register"
                )
                ac.regConfig.registerOnAdd = False

            ac.mediaConfig.srtpUse = _srtp_use(cfg.srtp)
            ac.mediaConfig.srtpSecureSignaling = 0 if cfg.srtp != "mandatory" else 1

            # ICE only when a STUN server is configured. Earlier we
            # enabled ICE unconditionally to fix one-way audio behind
            # symmetric NAT. The cost was a 1-3 s candidate-gathering
            # delay before EVERY first INVITE -- noticeable on the
            # demo / typical LAN-to-LAN case where ICE provides no
            # benefit. ICE without STUN only knows host candidates,
            # which is the same routing PJSIP would do without ICE
            # anyway, so flipping the flag was pure latency cost.
            try:
                if cfg.stun_server:
                    ac.natConfig.iceEnabled = True
                    ac.natConfig.iceMaxHostCands = 32
                else:
                    ac.natConfig.iceEnabled = False
            except Exception:
                log.warning("ICE config not available on this pjsua2 build")

            if cfg.stun_server:
                ac.natConfig.sipStunUse = 1
                ac.natConfig.mediaStunUse = 1
            return ac

else:

    class SipAccount:  # type: ignore[no-redef]
        def __init__(self, *a, **kw) -> None:
            raise RuntimeError("pjsua2 not available")
