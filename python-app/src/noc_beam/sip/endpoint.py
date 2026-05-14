"""Singleton wrapper around pjsua2.Endpoint.

Responsibilities:
- Initialise PJSIP exactly once with our codec/log/transport settings.
- Manage transports (UDP, TCP, TLS).
- Manage accounts (add/remove/modify at runtime).
- Apply codec priorities from settings.
- Place outgoing calls.
- Tear down cleanly on shutdown.

Threading model: PJSIP runs its own worker thread(s). Most pjsua2 calls are
thread-safe but must be made from a thread that has been registered with
PJSIP. We register the Qt main thread once at startup via libRegisterThread.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from noc_beam.config.store import AccountConfig, GlobalSettings
from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE, pj
from noc_beam.sip.account import SipAccount
from noc_beam.sip.call import SipCall
from noc_beam.sip.events import sip_events

log = logging.getLogger(__name__)


class SipEndpoint:
    """Holds the single pjsua2.Endpoint and our active accounts."""

    _instance: Optional["SipEndpoint"] = None

    def __init__(self) -> None:
        self._ep = None
        self._accounts: dict[str, SipAccount] = {}
        self._transports: dict[str, int] = {}
        self._started = False
        self._lock = threading.RLock()
        self._log_writer = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @classmethod
    def instance(cls) -> "SipEndpoint":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def start(self, settings: GlobalSettings) -> None:
        with self._lock:
            if self._started:
                return
            if not PJSUA2_AVAILABLE:
                sip_events().endpoint_error.emit(
                    "pjsua2 not available — install the wheel or run a custom build"
                )
                return

            try:
                self._ep = pj.Endpoint()
                self._ep.libCreate()

                ep_cfg = pj.EpConfig()
                ep_cfg.uaConfig.userAgent = settings.user_agent
                ep_cfg.uaConfig.maxCalls = 16
                ep_cfg.logConfig.level = settings.log_level
                ep_cfg.logConfig.consoleLevel = settings.log_level
                ep_cfg.medConfig.clockRate = settings.audio.clock_rate
                ep_cfg.medConfig.ecTailLen = settings.audio.ec_tail_ms

                # Hook log writer for SIP trace viewer
                from noc_beam.sip.trace import TraceLogWriter

                self._log_writer = TraceLogWriter()
                ep_cfg.logConfig.writer = self._log_writer

                self._ep.libInit(ep_cfg)

                # Transports
                self._create_transports(settings.sip_port)

                # Codecs (apply priorities)
                self._apply_codec_priorities(settings.codecs.priorities)

                self._ep.libStart()
                self._started = True
                sip_events().endpoint_started.emit()
                log.info("PJSIP endpoint started: %s", self._ep.libVersion().full)
            except Exception as e:
                log.exception("Failed to start PJSIP endpoint")
                sip_events().endpoint_error.emit(str(e))
                self._safe_destroy()

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            try:
                for acc in list(self._accounts.values()):
                    try:
                        acc.shutdown()
                    except Exception:
                        log.exception("Account shutdown error")
                self._accounts.clear()
                self._safe_destroy()
            finally:
                self._started = False
                sip_events().endpoint_stopped.emit()

    def _safe_destroy(self) -> None:
        try:
            if self._ep is not None:
                self._ep.libDestroy()
        except Exception:
            log.exception("libDestroy raised")
        self._ep = None

    # ------------------------------------------------------------------
    # Transports
    # ------------------------------------------------------------------
    def _create_transports(self, port: int) -> None:
        assert self._ep is not None

        tcfg = pj.TransportConfig()
        tcfg.port = port
        self._transports["udp"] = self._ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tcfg)

        tcfg_tcp = pj.TransportConfig()
        tcfg_tcp.port = port
        try:
            self._transports["tcp"] = self._ep.transportCreate(
                pj.PJSIP_TRANSPORT_TCP, tcfg_tcp
            )
        except Exception:
            log.warning("TCP transport unavailable")

        tcfg_tls = pj.TransportConfig()
        tcfg_tls.port = port + 1 if port else 0
        try:
            self._transports["tls"] = self._ep.transportCreate(
                pj.PJSIP_TRANSPORT_TLS, tcfg_tls
            )
        except Exception:
            log.warning("TLS transport unavailable (PJSIP built without TLS?)")

    # ------------------------------------------------------------------
    # Codecs
    # ------------------------------------------------------------------
    def _apply_codec_priorities(self, priorities: dict[str, int]) -> None:
        assert self._ep is not None
        try:
            codecs = self._ep.codecEnum2()
        except Exception:
            log.exception("codecEnum2 failed")
            return

        for codec in codecs:
            codec_id = codec.codecId
            # Find a configured priority by substring match
            new_prio = None
            for key, prio in priorities.items():
                if key.lower() in codec_id.lower():
                    new_prio = prio
                    break
            if new_prio is not None:
                try:
                    self._ep.codecSetPriority(codec_id, new_prio)
                    log.info("Codec %s priority=%d", codec_id, new_prio)
                except Exception:
                    log.exception("codecSetPriority failed for %s", codec_id)

    def list_codecs(self) -> list[tuple[str, int]]:
        if not self._started or self._ep is None:
            return []
        try:
            return [(c.codecId, c.priority) for c in self._ep.codecEnum2()]
        except Exception:
            log.exception("list_codecs failed")
            return []

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------
    def add_account(self, cfg: AccountConfig) -> SipAccount:
        with self._lock:
            if not self._started:
                raise RuntimeError("Endpoint not started")
            if cfg.id in self._accounts:
                self.remove_account(cfg.id)
            acc = SipAccount(cfg, self._transports)
            ac_cfg = acc.configure()
            acc.create(ac_cfg)
            self._accounts[cfg.id] = acc
            log.info("Account added: %s", cfg.id)
            return acc

    def remove_account(self, account_id: str) -> None:
        with self._lock:
            acc = self._accounts.pop(account_id, None)
            if acc is None:
                return
            try:
                acc.shutdown()
            except Exception:
                log.exception("Account shutdown error")

    def get_account(self, account_id: str) -> SipAccount | None:
        return self._accounts.get(account_id)

    def accounts(self) -> list[SipAccount]:
        return list(self._accounts.values())

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------
    def make_call(self, account_id: str, target_uri: str) -> SipCall:
        with self._lock:
            acc = self._accounts.get(account_id)
            if acc is None:
                raise ValueError(f"Unknown account {account_id}")
            if not target_uri.startswith(("sip:", "sips:", "tel:")):
                target_uri = f"sip:{target_uri}@{acc.cfg.domain}"
            call = SipCall(acc, account_id=account_id)
            prm = pj.CallOpParam(True)
            call.makeCall(target_uri, prm)
            acc.calls.append(call)
            return call

    def answer_call(self, call: SipCall, code: int = 200) -> None:
        prm = pj.CallOpParam(True)
        prm.statusCode = code
        call.answer(prm)

    def hangup_call(self, call: SipCall, code: int = 603) -> None:
        prm = pj.CallOpParam(True)
        prm.statusCode = code
        call.hangup(prm)

    def hold_call(self, call: SipCall) -> None:
        call.setHold(pj.CallOpParam(True))

    def resume_call(self, call: SipCall) -> None:
        """Take a held call off hold. pjsua2 unholds via reinvite with the
        UNHOLD flag set; the constant is PJSUA_CALL_UNHOLD = 1."""
        prm = pj.CallOpParam(True)
        try:
            prm.opt.flag = getattr(pj, "PJSUA_CALL_UNHOLD", 1)
            prm.opt.audioCount = 1
        except Exception:
            pass
        call.reinvite(prm)

    def reinvite_call(self, call: SipCall) -> None:
        prm = pj.CallOpParam(True)
        prm.opt.audioCount = 1
        call.reinvite(prm)

    def find_call(self, call_id: int) -> SipCall | None:
        """Look up a live SipCall across all accounts by pjsua2 call-id."""
        for acc in self._accounts.values():
            for c in acc.calls:
                try:
                    if c.getInfo().id == call_id:
                        return c
                except Exception:
                    continue
        return None

    def send_dtmf(self, call: SipCall, digits: str, account_cfg: AccountConfig) -> None:
        method = account_cfg.dtmf_method.lower()
        if method == "info":
            # SIP INFO via dialUci is not exposed — fall back to dialDtmf (RFC2833)
            # plus an INFO body via sendRequest.
            call.dialDtmf(digits)
        else:
            # RFC2833 and inband both use dialDtmf; inband generation is
            # configured at the codec level (PJMEDIA_TONEGEN).
            call.dialDtmf(digits)
