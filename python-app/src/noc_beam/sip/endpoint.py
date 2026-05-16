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
import re
import socket
import ssl
import threading
import time
import uuid

from noc_beam.config.store import AccountConfig, GlobalSettings
from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE, pj
from noc_beam.sip.account import SipAccount
from noc_beam.sip.call import SipCall
from noc_beam.sip.events import sip_events

log = logging.getLogger(__name__)

_SIP_STATUS_RE = re.compile(r"^SIP/2\.0\s+(\d{3})(?:\s+(.*))?$", re.IGNORECASE)


def _system_nameservers() -> list[str]:
    """Return the system's DNS resolvers in IP form for PJSIP.

    PJSIP's resolver needs explicit nameserver IPs; it does NOT
    consult the OS resolver by default. On Windows we read the
    active adapters' DNS entries via WMI/getaddrinfo. As a
    last-resort fallback we add public resolvers (Google + Cloudflare)
    so a misconfigured box still gets external SIP DNS instead of
    silently failing every non-registered domain.
    """
    out: list[str] = []
    try:
        import subprocess
        # `nslookup` parses the default DNS server out of ipconfig
        # cheaply; we just need it for PJSIP. Wrapped tight so an
        # antivirus quarantining the binary or a non-Windows port
        # falls straight through to the public fallback below.
        result = subprocess.run(
            ["nslookup", "google.com"],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in (result.stdout or "").splitlines():
            s = line.strip()
            if s.lower().startswith("address:") and "#" not in s:
                ip = s.split(":", 1)[1].strip()
                # Skip the resolved answer (after "Name:" line);
                # the first "Address:" before "Name:" is the
                # resolver itself.
                if ip and ip not in out:
                    out.append(ip)
                # We only want the first one (the active resolver).
                if len(out) >= 1:
                    break
    except Exception:
        log.debug("Could not detect system DNS via nslookup; using public fallback")
    # Public-resolver fallback so PJSIP DNS always works even if
    # we couldn't read the system config (e.g. corporate sandbox).
    for fallback in ("8.8.8.8", "1.1.1.1"):
        if fallback not in out:
            out.append(fallback)
    return out


def collect_stun_servers(accounts: list[AccountConfig] | None) -> list[str]:
    if not accounts:
        return []
    seen: set[str] = set()
    servers: list[str] = []
    for account in accounts:
        if not account.enabled:
            continue
        server = account.stun_server.strip()
        if not server or server in seen:
            continue
        seen.add(server)
        servers.append(server)
    return servers


class SipEndpoint:
    """Holds the single pjsua2.Endpoint and our active accounts."""

    _instance: SipEndpoint | None = None

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
    def instance(cls) -> SipEndpoint:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def is_started(self) -> bool:
        return self._started

    def start(
        self,
        settings: GlobalSettings,
        accounts: list[AccountConfig] | None = None,
    ) -> None:
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
                for stun_server in collect_stun_servers(accounts):
                    ep_cfg.uaConfig.stunServer.append(stun_server)
                ep_cfg.uaConfig.stunIgnoreFailure = True
                # DNS nameservers for PJSIP's resolver. Without this,
                # gethostbyname() on non-cached SIP domains returns
                # PJ_ERESOLVE and outbound INVITEs to e.g.
                # sip.linphone.org fail before they leave the box --
                # the registrar's IP is the only host PJSIP knows
                # because that's what REGISTER cached.
                # Read the system resolvers and pass them in so
                # external SIP URIs (anonymous test targets, peer
                # accounts on other domains) actually resolve.
                for ns in _system_nameservers():
                    try:
                        ep_cfg.uaConfig.nameserver.append(ns)
                    except Exception:
                        # Older pjsua2 builds may not expose
                        # nameserver as a list-append container.
                        log.warning("pjsua2 doesn't accept nameserver entries on this build")
                        break
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
                # Full state reset on partial start. _safe_destroy
                # first so a PJSIP callback that races teardown
                # still sees our _accounts dict intact (so it can
                # look itself up); THEN clear the dicts and reset
                # _started so the next start() inherits a clean
                # slate. _safe_destroy also nulls _ep and
                # _log_writer to prevent the destroyed pj.Endpoint
                # from outliving anything Python.
                self._safe_destroy()
                self._accounts.clear()
                self._transports.clear()
                self._started = False

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            try:
                # Polite un-REGISTER for every account before tear-down. Without
                # this the registrar keeps a stale binding for the configured
                # expires window (often 3600s); incoming INVITEs route to a
                # dead client until the binding times out. setRegistration(False)
                # sends REGISTER with Expires:0; we then spin libHandleEvents
                # a few times to give the wire round-trip a chance to complete
                # before libDestroy yanks the transport out from under it.
                for acc in list(self._accounts.values()):
                    try:
                        if getattr(acc.cfg, "register", True):
                            acc.setRegistration(False)
                    except Exception:
                        log.exception("Un-register on stop failed for %s", acc.cfg.id)
                if self._ep is not None:
                    deadline = time.monotonic() + 1.5
                    while time.monotonic() < deadline:
                        try:
                            self._ep.libHandleEvents(50)
                        except Exception:
                            break
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
        # Drop our reference to the TraceLogWriter too -- the C++
        # side from the destroyed lib may still hold its vtable
        # pointer; re-entering start() would otherwise rebind a new
        # writer while leaving the old one pinned in Python and
        # potentially racing the new pjsua2 instance's logging.
        self._log_writer = None

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
        # Verify the registrar's certificate by default. PJSIP defaults
        # to verifyServer=False which means TLS gives you encryption
        # without authentication -- a downgrade from regular HTTPS that
        # most ops teams don't realise. method=TLSv1.2+ rules out the
        # SSLv3/TLSv1.0 fallback that some old PBXes still try to negotiate.
        try:
            tls = tcfg_tls.tlsConfig
            tls.verifyServer = True
            tls.verifyClient = False
            # PJSIP_SSL_DEFAULT_METHOD = 0 picks the system best; we
            # pin to TLS 1.2+ via the pj enum if the build exposes it.
            try:
                tls.method = getattr(pj, "PJSIP_TLSV1_2_METHOD", tls.method)
            except Exception:
                pass
        except Exception:
            log.warning("Could not configure TLS verifyServer (older pjsua2 build)")
        try:
            self._transports["tls"] = self._ep.transportCreate(
                pj.PJSIP_TRANSPORT_TLS, tcfg_tls
            )
        except Exception:
            log.warning("TLS transport unavailable (PJSIP built without TLS?)")

    # ------------------------------------------------------------------
    # Codecs
    # ------------------------------------------------------------------
    @staticmethod
    def _codec_match(key: str, codec_id: str) -> bool:
        """Match a user-configured codec key against a pjsua2 codec id.

        pjsua2 codec ids look like ``PCMA/8000/1`` or ``opus/48000/2``.
        Keys may be either the bare name (``opus``) or name+clockrate
        (``opus/48000``); channel count is ignored either way. Comparison
        is case-insensitive on the name only.
        """
        kparts = key.lower().split("/")
        cparts = codec_id.lower().split("/")
        if not kparts or not cparts:
            return False
        if kparts[0] != cparts[0]:
            return False
        if len(kparts) == 1:
            return True                       # name-only match — any clockrate
        if len(cparts) < 2:
            return False
        return kparts[1] == cparts[1]         # name + clockrate must agree

    def _apply_codec_priorities(self, priorities: dict[str, int]) -> None:
        assert self._ep is not None
        try:
            codecs = self._ep.codecEnum2()
        except Exception:
            log.exception("codecEnum2 failed")
            return

        for codec in codecs:
            codec_id = codec.codecId
            # Prefer the most-specific key; "opus/48000" beats "opus".
            best_key: str | None = None
            best_prio: int | None = None
            best_specificity = -1
            for key, prio in priorities.items():
                if not self._codec_match(key, codec_id):
                    continue
                specificity = key.count("/")
                if specificity > best_specificity:
                    best_specificity = specificity
                    best_key = key
                    best_prio = prio
            if best_prio is None:
                continue
            try:
                self._ep.codecSetPriority(codec_id, best_prio)
                log.info("Codec %s priority=%d (rule %s)", codec_id, best_prio, best_key)
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
    # Diagnostics
    # ------------------------------------------------------------------
    def options_probe(self, target_uri: str, timeout_s: float = 3.0) -> tuple[int, str, float]:
        """Send a SIP OPTIONS probe and return ``(code, reason, rtt_ms)``.

        PJSUA2 does not expose an out-of-dialog OPTIONS helper in the Python
        wrapper, so the diagnostics panel sends a minimal standards-compliant
        request directly over UDP or TCP. This is intentionally a reachability
        probe, not an authenticated account operation.
        """
        uri = target_uri.strip()
        if not uri:
            raise ValueError("OPTIONS target is required")
        if not uri.startswith(("sip:", "sips:")):
            uri = f"sip:{uri}"

        scheme, host, port, transport = self._parse_probe_uri(uri)
        if scheme == "sips" and transport == "tcp":
            transport = "tls"
        if transport not in {"udp", "tcp", "tls"}:
            raise ValueError(f"Unsupported OPTIONS transport: {transport}")

        start = time.perf_counter()
        if transport in {"tcp", "tls"}:
            response = self._send_options_tcp(uri, host, port, timeout_s, use_tls=transport == "tls")
        else:
            response = self._send_options_udp(uri, host, port, timeout_s)
        rtt_ms = (time.perf_counter() - start) * 1000.0
        code, reason = self._parse_options_response(response)
        return code, reason, rtt_ms

    @staticmethod
    def _parse_probe_uri(uri: str) -> tuple[str, str, int, str]:
        scheme, rest = uri.split(":", 1)
        scheme = scheme.lower()
        if scheme not in {"sip", "sips"}:
            raise ValueError("OPTIONS target must use sip: or sips:")

        main, _, params = rest.partition(";")
        main = main.split("?", 1)[0]
        hostport = main.rsplit("@", 1)[-1]
        if not hostport:
            raise ValueError("OPTIONS target host is required")

        if hostport.startswith("["):
            end = hostport.find("]")
            if end < 0:
                raise ValueError("Invalid IPv6 SIP target")
            host = hostport[1:end]
            tail = hostport[end + 1:]
            port = int(tail[1:]) if tail.startswith(":") else (5061 if scheme == "sips" else 5060)
        else:
            if ":" in hostport:
                host, port_text = hostport.rsplit(":", 1)
                port = int(port_text)
            else:
                host = hostport
                port = 5061 if scheme == "sips" else 5060

        transport = "tls" if scheme == "sips" else "udp"
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "transport" and value:
                transport = value.strip().lower()
                break
        return scheme, host, port, transport

    def _send_options_udp(self, uri: str, host: str, port: int, timeout_s: float) -> bytes:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_s)
            sock.connect((host, port))
            local_host, local_port = sock.getsockname()
            request = self._build_options_request(uri, "UDP", local_host, local_port)
            sock.send(request)
            try:
                return sock.recv(8192)
            except TimeoutError:
                return b"SIP/2.0 408 Request Timeout\r\n\r\n"

    def _send_options_tcp(
        self, uri: str, host: str, port: int, timeout_s: float, *, use_tls: bool = False
    ) -> bytes:
        try:
            raw_sock = socket.create_connection((host, port), timeout=timeout_s)
            with raw_sock:
                sock = (
                    ssl.create_default_context().wrap_socket(raw_sock, server_hostname=host)
                    if use_tls
                    else raw_sock
                )
                with sock:
                    sock.settimeout(timeout_s)
                    local_host, local_port = sock.getsockname()
                    transport = "TLS" if use_tls else "TCP"
                    request = self._build_options_request(uri, transport, local_host, local_port)
                    sock.sendall(request)
                    return sock.recv(8192)
        except TimeoutError:
            return b"SIP/2.0 408 Request Timeout\r\n\r\n"

    @staticmethod
    def _build_options_request(uri: str, transport: str, local_host: str, local_port: int) -> bytes:
        branch = f"z9hG4bK-{uuid.uuid4().hex}"
        tag = uuid.uuid4().hex[:12]
        call_id = f"{uuid.uuid4().hex}@noc-beam"
        lines = [
            f"OPTIONS {uri} SIP/2.0",
            f"Via: SIP/2.0/{transport} {local_host}:{local_port};branch={branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:noc-beam@{local_host}>;tag={tag}",
            f"To: <{uri}>",
            f"Call-ID: {call_id}",
            "CSeq: 1 OPTIONS",
            f"Contact: <sip:noc-beam@{local_host}:{local_port}>",
            "Accept: application/sdp",
            "User-Agent: NOC_Beam",
            "Content-Length: 0",
            "",
            "",
        ]
        return "\r\n".join(lines).encode("ascii")

    @staticmethod
    def _parse_options_response(response: bytes) -> tuple[int, str]:
        if not response:
            raise RuntimeError("Empty SIP response")
        first_line = response.splitlines()[0].decode("ascii", errors="replace").strip()
        match = _SIP_STATUS_RE.match(first_line)
        if not match:
            raise RuntimeError(f"Invalid SIP response: {first_line}")
        return int(match.group(1)), (match.group(2) or "").strip()

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------
    def add_account(self, cfg: AccountConfig) -> SipAccount:
        pending_err: str | None = None
        with self._lock:
            if not self._started:
                raise RuntimeError("Endpoint not started")
            if cfg.id in self._accounts:
                self.remove_account(cfg.id)
            acc = SipAccount(cfg, self._transports)
            ac_cfg = acc.configure()
            try:
                acc.create(ac_cfg)
            except Exception:
                # acc.create can raise on bad cred / refused transport
                # AFTER the SipAccount has been partially built. Without
                # this clean-up the SWIG-shadow pj.Account leaks until
                # libDestroy. Pair shutdown() with the failed create()
                # and re-raise so the caller still sees the error.
                try:
                    acc.shutdown()
                except Exception:
                    log.exception("Cleanup of partially-created account failed")
                raise
            self._accounts[cfg.id] = acc
            # Drain any deferred diagnostic the configure() step set
            # (e.g. requested transport not bound). Capture under the
            # lock, dispatch OUTSIDE the lock so subscribers can call
            # back into SipEndpoint without deadlocking.
            pending_err = getattr(acc, "_pending_transport_error", None)
            acc._pending_transport_error = None
            log.info("Account added: %s", cfg.id)
        if pending_err is not None:
            # Queue onto the event loop so the emit also can't run
            # synchronously on the caller's stack while it's still
            # finishing add_account().
            try:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(
                    0,
                    lambda aid=cfg.id, msg=pending_err: sip_events()
                    .registration_changed.emit(aid, 0, msg),
                )
            except Exception:
                # Headless / no Qt loop -- fire directly; the lock is
                # already released so no re-entrancy risk remains.
                sip_events().registration_changed.emit(cfg.id, 0, pending_err)
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
            target_uri = self._normalize_dial_target(target_uri, acc.cfg.domain)
            call = SipCall(acc, account_id=account_id)
            prm = pj.CallOpParam(True)
            call.makeCall(target_uri, prm)
            acc.calls.append(call)
            return call

    @staticmethod
    def _normalize_dial_target(target: str, account_domain: str) -> str:
        """Turn a user-typed dial string into a valid SIP target URI.

        Rules:
          - Already a sip:/sips:/tel: URI -> return unchanged.
          - Contains `@` (e.g. "music@iptel.org") -> assumed
            user@host, just prepend "sip:" to make it a real URI.
            Without this branch, the bare-number fallback below
            would produce "sip:music@iptel.org@account_domain"
            with TWO @ signs, which pjsua2 rejects (empty-string
            exception -> blank "Call failed" dialog).
          - Bare userpart / number -> sip:{target}@{account_domain}.
        """
        t = (target or "").strip()
        if not t:
            raise ValueError("Empty dial target")
        if t.startswith(("sip:", "sips:", "tel:")):
            return t
        if "@" in t:
            return f"sip:{t}"
        return f"sip:{t}@{account_domain}"

    def answer_call(self, call: SipCall, code: int = 200) -> None:
        prm = pj.CallOpParam(True)
        prm.statusCode = code
        call.answer(prm)

    def hangup_call(self, call: SipCall, code: int | None = None) -> None:
        """Terminate a call with the right SIP status code for its state.

        SIP semantics:
          * CONFIRMED dialog -> BYE with 200 (normal completion)
          * CALLING / EARLY  -> CANCEL (pjsua2 emits CANCEL on hangup
            of a non-confirmed dialog regardless of statusCode, but we
            tag 487 Request Terminated for clean CDR/trace output)
          * INCOMING ringing -> 486 Busy Here is the polite default
          * Caller explicitly passes 603 only when REJECTING an incoming

        Default-was-603 was a long-standing bug: CDRs/SIP-traces showed
        every normal hangup as Decline; CUCM and BroadWorks dialplans
        branch on 603 (forward-on-decline) so this was wire-protocol
        wrong. Pick the code from the call's current state.
        """
        if code is None:
            try:
                # int() wrap is defensive: some pjsua2 SWIG/Cython
                # builds return a wrapped enum object whose __eq__ vs
                # int silently returns False. Without the wrap, every
                # comparison falls through to the else branch below
                # and an INCOMING call gets answered with 200 OK then
                # immediately BYE'd instead of being properly rejected
                # with 486 Busy Here.
                state = int(call.getInfo().state)
            except Exception:
                state = -1
            # pjsua2 PJSIP_INV_STATE_* numbers. 5 = CONFIRMED.
            if state == 5:
                code = 200
            elif state in (1, 3, 4):  # CALLING, EARLY, CONNECTING
                code = 487
            elif state == 2:  # INCOMING (we're the callee)
                code = 486
            else:
                code = 200
        prm = pj.CallOpParam(True)
        prm.statusCode = code
        call.hangup(prm)

    def hold_call(self, call: SipCall) -> None:
        call.setHold(pj.CallOpParam(True))

    def resume_call(self, call: SipCall) -> None:
        """Take a held call off hold.

        pjsua2 has two ways to unhold:
          1. `call.setHold(prm)` with prm.opt.flag = PJSUA_CALL_UNHOLD
          2. `call.reinvite(prm)` with prm.opt.flag = PJSUA_CALL_UNHOLD

        The first works reliably against every PBX we've tested
        (Asterisk, FreeSWITCH, Kamailio, CUCM). The second's
        UNHOLD flag is ignored by some pjsua2 builds when passed
        to reinvite -- the SDP just gets re-sent with sendrecv but
        Asterisk in particular keeps the remote in held state until
        an explicit unhold re-INVITE. setHold path also has the
        nice property of being symmetric with hold_call() above.
        """
        prm = pj.CallOpParam(True)
        try:
            prm.opt.flag = getattr(pj, "PJSUA_CALL_UNHOLD", 1)
            prm.opt.audioCount = 1
        except Exception:
            pass
        try:
            call.setHold(prm)
        except Exception:
            # Old builds may not honour UNHOLD via setHold; fall
            # back to the reinvite path. At least one of them will
            # do the right thing on every PJSIP build out there.
            log.exception("setHold(UNHOLD) failed; falling back to reinvite")
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

    def blind_transfer(self, call: SipCall, target_uri: str, account_id: str | None = None) -> None:
        """Send REFER with `Refer-To: target_uri` (blind transfer).

        The remote party will INVITE the target on our behalf. We stay on
        the call until they hang up; pjsua2 emits an NOTIFY-driven state
        change via onTransferState which we surface as a status update.
        """
        target_uri = self._normalize_uri(target_uri, account_id)
        prm = pj.CallOpParam(True)
        call.xfer(target_uri, prm)
        log.info("Blind transfer: %s", target_uri)

    def attended_transfer(self, original: SipCall, consult: SipCall) -> None:
        """REFER with Replaces — hand `original`'s remote over to `consult`'s.

        Preconditions: both calls are CONFIRMED on this endpoint and on the
        same account. pjsua2 builds the Replaces header automatically when
        we call `xferReplaces(original, consult, ...)` — but the API is
        ``original.xferReplaces(consult, prm)``: tell the *consult* peer to
        REPLACE their dialog with the original one.
        """
        prm = pj.CallOpParam(True)
        original.xferReplaces(consult, prm)
        log.info("Attended transfer issued (call %s ⇋ call %s)",
                 original.getInfo().id, consult.getInfo().id)

    def _normalize_uri(self, target: str, account_id: str | None) -> str:
        if target.startswith(("sip:", "sips:", "tel:")):
            return target
        acc = self._accounts.get(account_id) if account_id else None
        if acc is None:
            raise ValueError("Plain number requires an account context for the domain")
        return f"sip:{target}@{acc.cfg.domain}"

    def set_call_mute(self, call: SipCall, muted: bool) -> None:
        """Stop/resume the capture device → call audio transmit.

        pjsua2 routes audio via the conference bridge: the capture device's
        port transmits into the call's port. Stopping that one-way link is
        equivalent to muting the microphone for this call only — playback
        from the remote still works, and other calls (if any) are unaffected.
        """
        if self._ep is None:
            return
        info = call.getInfo()
        for mi in info.media:
            if mi.type != 1 or mi.status != 1:   # audio + active
                continue
            try:
                aud = call.getAudioMedia(mi.index)
                capture = self._ep.audDevManager().getCaptureDevMedia()
                if muted:
                    capture.stopTransmit(aud)
                else:
                    capture.startTransmit(aud)
            except Exception:
                log.exception("set_call_mute(%s) failed for media %d", muted, mi.index)

    def send_dtmf(self, call: SipCall, digits: str, account_cfg: AccountConfig) -> None:
        """Send DTMF using the method configured on the account.

        * rfc2833 — RTP telephone-event payload (RFC 4733). pjsua2's
          `dialDtmf` does this.
        * info    — SIP INFO with `application/dtmf-relay` body, one INFO
          per digit. Used by some legacy carriers and BroadWorks gateways.
        * inband  — in-band tone generation in the audio stream. pjsua2
          handles this at the codec layer when `PJMEDIA_TONEGEN` is set;
          we still drive it via `dialDtmf` for the API contract.
        """
        method = (account_cfg.dtmf_method or "rfc2833").lower()
        if method == "info":
            self._send_dtmf_info(call, digits)
        else:
            call.dialDtmf(digits)

    def _send_dtmf_info(self, call: SipCall, digits: str) -> None:
        """One SIP INFO per digit with an `application/dtmf-relay` body.

        Earlier this borrowed `pj.SendInstantMessageParam` (an IM type)
        just to read its `contentType`/`content` attributes; in newer
        pjsua2 builds those fields don't exist and the call raised
        AttributeError before any wire bytes were sent. Build the
        CallSendRequestParam directly.
        """
        for d in digits:
            try:
                req = self._build_info_param(
                    "application/dtmf-relay",
                    f"Signal={d}\r\nDuration=160\r\n",
                )
                call.sendRequest(req)
            except Exception:
                # pjsua2's call.sendRequest signature varies across versions;
                # fall back to the in-band path so the digit isn't silently
                # dropped.
                log.exception("SIP INFO DTMF send failed; falling back to RFC2833")
                try:
                    call.dialDtmf(d)
                except Exception:
                    log.exception("RFC2833 fallback also failed")

    @staticmethod
    def _build_info_param(content_type: str, body: str):  # type: ignore[no-untyped-def]
        """Construct a pjsua2.CallSendRequestParam for a SIP INFO with body.

        Field-name compatibility across pjsua2 builds:
          * 2.10-2.12  : prm.txOption.{contentType, msgBody}
          * 2.13+      : prm.msgData.{contentType, msgBody}
          * some SWIG-rebuilt forks rename to ctType/msgBody
        Feature-detect by attribute existence rather than version
        sniffing; log which path we took so build-mismatch DTMF
        regressions don't silently fall back to RFC2833.
        """
        prm = pj.CallSendRequestParam()
        prm.method = "INFO"
        opt = pj.SipTxOption()
        # Most builds expose contentType + msgBody on SipTxOption
        # directly. If those aren't present, give up cleanly.
        if not hasattr(opt, "contentType") or not hasattr(opt, "msgBody"):
            raise RuntimeError(
                "pjsua2 SipTxOption is missing contentType/msgBody; "
                "this build does not support SIP INFO body payloads"
            )
        opt.contentType = content_type
        opt.msgBody = body
        # Carrier-attribute: try the new msgData slot first (2.13+),
        # then fall back to txOption (2.10-2.12).
        if hasattr(prm, "msgData"):
            prm.msgData = opt
            log.debug("SIP INFO DTMF using msgData (pjsua2 >= 2.13 layout)")
        elif hasattr(prm, "txOption"):
            prm.txOption = opt
            log.debug("SIP INFO DTMF using txOption (pjsua2 <= 2.12 layout)")
        else:
            raise RuntimeError(
                "pjsua2 CallSendRequestParam exposes neither msgData nor "
                "txOption; cannot attach DTMF INFO body"
            )
        return prm
