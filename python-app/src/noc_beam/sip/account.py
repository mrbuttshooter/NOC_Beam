"""Account wrapper around pjsua2.Account."""
from __future__ import annotations

import logging

from noc_beam.config.store import AccountConfig
from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE, pj
from noc_beam.sip.call import SipCall
from noc_beam.sip.events import sip_events
from noc_beam.sip.netselect import effective_transport_for_account, local_address_for_account

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


def _normalize_proxy_uri(proxy: str, scheme: str) -> str:
    """Return a PJSIP route URI for the optional outbound proxy field."""
    value = (proxy or "").strip()
    if not value:
        return ""
    if value.lower().startswith(("sip:", "sips:")):
        return value
    return f"{scheme}:{value}"


def _contact_uri_params_for_transport(transport: str) -> str:
    key = (transport or "udp").lower()
    if key == "tcp":
        return ";transport=TCP"
    if key == "tls":
        return ";transport=TLS"
    return ""


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
            # Append to self.calls ONLY after construction + getInfo() +
            # signal emit all succeed. Previously, the append happened
            # eagerly and a later exception left a half-initialised SipCall
            # in self.calls -- PJSIP couldn't clean up its native side and
            # the next find_call() iteration could deref a broken wrapper.
            try:
                call = SipCall(self, prm.callId, self.cfg.id)
                info = call.getInfo()
                sip_events().call_incoming.emit(
                    self.cfg.id, info.id, info.remoteUri, True
                )
                self.calls.append(call)
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
            transport = effective_transport_for_account(cfg)
            if transport != (cfg.transport or "udp").lower():
                log.info(
                    "Account %s using %s transport for %s switch compatibility",
                    cfg.id, transport.upper(), cfg.switch_type,
                )
            scheme = "sips" if transport == "tls" else "sip"
            transport_param = f";transport={transport}" if transport in ("tcp", "tls") else ""
            # If display_name is set, render the SIP id-URI as
            #   "Display Name" <sip:user@host;transport=tcp>
            # so carriers that read the A-number from the From: header's
            # display string (Eyebeam wire compatibility) see it. We
            # strip any embedded " or angle-bracket chars to keep the
            # header well-formed.
            # Defensive: if username (or auth_user) still carries an
            # unsubstituted `{id}` placeholder from a legacy bad supplier
            # swap, sanitize it. Curly braces are RFC-3261 reserved
            # characters not allowed in SIP user-part; passing them to
            # pjsua_acc_add raises PJSIP_EINVALIDURI silently. We:
            #   1. Try to substitute {id} from auth_user (if it doesn't
            #      itself contain {id})
            #   2. Otherwise strip the {id} placeholder (logged loud so
            #      the operator fixes their config)
            def _sanitize_userpart(value: str, fallback: str = "") -> str:
                if not value or "{" not in value:
                    return value
                if fallback and "{" not in fallback:
                    # Substitute {id} with the fallback's digit portion if
                    # reasonable; otherwise use the fallback verbatim.
                    # Strip a SINGLE leading carrier 'U' only when it's the
                    # "U<digits>" UID pattern (e.g. "U080" -> "080"). The
                    # old lstrip("Uu") stripped ALL leading U/u, so a real
                    # auth_user like "User123" became "ser123" -- a silent
                    # wrong-identity substitution.
                    sub = fallback
                    if len(fallback) > 1 and fallback[0] in "Uu" and fallback[1:].isdigit():
                        sub = fallback[1:]
                    return value.replace("{id}", sub) if "{id}" in value else fallback
                # No useful fallback — strip the placeholder. Refuse the
                # "anonymous" fallback: that was registering accounts as
                # a literal user "anonymous" against the carrier,
                # silently misrepresenting identity. If we land here both
                # username and auth_user contain unsubstituted {id} AND
                # there's nothing to substitute from — return empty so
                # the caller (Account.create) fails loudly instead of
                # silently authenticating as the wrong identity.
                stripped = (
                    value.replace("{id}", "")
                         .replace("{", "")
                         .replace("}", "")
                )
                return stripped  # may be empty -> caller errors out clearly

            _orig_user = cfg.username
            _orig_auth = cfg.auth_user
            _safe_user = _sanitize_userpart(cfg.username, cfg.auth_user)
            _safe_auth = _sanitize_userpart(cfg.auth_user, _safe_user)
            if _safe_user != _orig_user or _safe_auth != _orig_auth:
                log.warning(
                    "Account %s username/auth_user contained unsubstituted "
                    "{id} placeholder: user %r->%r, auth %r->%r. "
                    "Please open Edit Account and set Username to the "
                    "actual carrier UID (e.g. 'U080'), not a template.",
                    cfg.id, _orig_user, _safe_user, _orig_auth, _safe_auth,
                )
            _bare_uri = f"{scheme}:{_safe_user}@{host}{transport_param}"
            # idUri MUST be the bare URI for pjsua2.Account.create() —
            # newer PJSIP builds reject the "name-addr" form ("Display"
            # <sip:user@host>) with PJSIP_EINVALIDURI (status 171039)
            # even though that form is valid SIP name-addr syntax.
            #
            # DO NOT touch ac.regConfig.contactParams here either — setting
            # it to an empty string causes a second PJSIP_EINVALIDURI on
            # the next pjsua_acc_add call because pjsua appends the empty
            # param string to the Contact URI, producing a malformed
            # `<sip:user@host;>` (trailing semicolon) at parse time.
            # The display name in From is a known regression of this fix;
            # if the carrier needs the A-number in From: header for
            # billing, the right path is to set the full URI via a custom
            # header rather than via contactParams.
            ac.idUri = _bare_uri
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
            # Use the "*" wildcard realm so the digest credential matches
            # whatever realm the registrar/proxy challenges with. Pinning
            # the realm to the account domain caused an un-retryable 401
            # loop whenever a carrier challenged with a realm != domain
            # (very common: an SBC's realm rarely equals the SIP domain).
            # For a NOC tool talking to a known, operator-configured set of
            # carriers, the off-path credential-leak risk of "*" is the
            # lesser evil versus a registration that can never succeed.
            realm = "*"
            # Only attach digest credentials when the operator provided a
            # password. Wholesale carriers often use IP-based authentication
            # (the carrier's edge whitelists the operator's source IP and
            # accepts unauthenticated REGISTER + INVITE). Unconditionally
            # creating an AuthCredInfo with empty password caused pjsua2
            # Account.create() to reject the entire config silently --
            # pjsua2.Error() with no message -- because AuthCredInfo is
            # validated at construction or attach time.
            #
            # When password is empty:
            #   * skip authCreds entirely; PJSIP won't offer a 401 response
            #     to challenges (carrier shouldn't be sending them anyway)
            #   * REGISTER still goes out (regConfig.registerOnAdd above);
            #     the carrier either 200-OKs by IP or 401s and we surface
            #     that to the user
            if (cfg.password or "").strip():
                # Use sanitized values so the auth cred matches what's in
                # the From: URI. If both are still empty/braced after
                # sanitize, pjsua AuthCredInfo will refuse — that's fine,
                # we'd rather see that than a silent Invalid URI.
                cred = pj.AuthCredInfo(
                    "digest", realm, _safe_auth or _safe_user, 0, cfg.password
                )
                ac.sipConfig.authCreds.append(cred)
            else:
                log.info(
                    "Account %s has no password -- assuming IP-based auth, "
                    "skipping AuthCredInfo append", cfg.id,
                )

            proxy_uri = _normalize_proxy_uri(cfg.proxy, scheme)
            if proxy_uri:
                ac.sipConfig.proxies.append(proxy_uri)
            contact_uri_params = _contact_uri_params_for_transport(transport)
            if contact_uri_params:
                ac.sipConfig.contactUriParams = contact_uri_params

            try:
                ac.natConfig.sipOutboundUse = 0
            except Exception:
                log.warning("SIP outbound disable not available on this pjsua2 build")

            tid = _transport_id_for(transport, self._transports)
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
                    cfg.id, transport,
                )
                self._pending_transport_error = (
                    f"Transport '{transport}' unavailable; "
                    f"account will not register"
                )
                ac.regConfig.registerOnAdd = False

            ac.mediaConfig.srtpUse = _srtp_use(cfg.srtp)
            ac.mediaConfig.srtpSecureSignaling = 0 if cfg.srtp != "mandatory" else 1
            # Set the publicAddress on mediaConfig so RTP c=IN IP4 advertises
            # the outbound-reachable address. Empty string means netselect
            # couldn't resolve / no route — do NOT call setter, since some
            # pjsua2 builds normalise empty to 0.0.0.0 which guarantees the
            # one-way-audio bug this whole code path exists to prevent.
            try:
                media_address = local_address_for_account(cfg)
                if media_address:
                    ac.mediaConfig.transportConfig.publicAddress = media_address
                    log.info(
                        "Account %s advertising media address %s",
                        cfg.id, media_address,
                    )
                else:
                    # WARNING (not DEBUG) so support bundles surface this —
                    # silent fallback was regressing the NAT fix without
                    # the operator noticing one-way audio until a real call.
                    log.warning(
                        "Account %s: netselect returned no usable local "
                        "address; RTP publicAddress NOT set. Calls behind "
                        "symmetric NAT may have one-way audio. Check that "
                        "domain %s resolves and is routable.",
                        cfg.id, cfg.domain,
                    )
            except Exception:
                log.warning(
                    "Account %s: could not select media advertised address: %r",
                    cfg.id, cfg,
                )

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
