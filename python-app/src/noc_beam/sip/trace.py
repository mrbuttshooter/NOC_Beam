"""SIP message trace capture.

PJSIP exposes signaling via the LogWriter callback (logs include the wire
text) and via pjsip module callbacks. The cleanest cross-version approach is
to parse the log stream: lines that start with .SIP.. or contain SIP method
verbs at column 0 of a multi-line block.

We keep this simple: any log line beginning with one of the SIP methods or
"SIP/2.0" is treated as the start of a signaling message; subsequent lines
until a blank line are body.
"""
from __future__ import annotations

import logging
import re
import time

from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE, pj
from noc_beam.sip.events import sip_events

log = logging.getLogger(__name__)

_SIP_START = re.compile(
    r"^(INVITE|REGISTER|ACK|BYE|CANCEL|OPTIONS|SUBSCRIBE|NOTIFY|REFER|MESSAGE|PUBLISH|INFO|UPDATE|PRACK)\s+sip[s]?:|^SIP/2\.0\s+\d{3}"
)

# --- PII / credential redaction ----------------------------------------
# SIP traces written to disk OR shipped in support bundles must not
# carry digest-auth material or unredacted user identifiers. The full-
# wire trace lives on the in-app trace window for live debugging; this
# layer masks anything that goes through `sip_message` so subscribers
# (and any logger that mirrors them to disk) get a redacted body.
#
# Redaction is bypassable via a per-process "diagnostic mode" toggle
# read from the SIP_TRACE_DIAGNOSTIC env var. The toggle is intended
# for short-window deep debugging sessions and the UI advertises that
# raw capture is on. Default = redacted.
import os as _os

_AUTH_HEADER_RE = re.compile(
    # Include RFC 3261 line folding: continuation lines start with SP/HT.
    # Without the trailing group a folded Authorization value spilled its
    # secret onto the next line and leaked unredacted.
    r"^(Authorization|Proxy-Authorization|WWW-Authenticate|Proxy-Authenticate)\s*:.*(?:\r?\n[ \t].*)*$",
    re.IGNORECASE | re.MULTILINE,
)
# Pulls username, response, nonce out of digest challenges so even if
# the line is reformatted across versions we still scrub the secrets.
# The value alternation matches a full quoted string ("...") OR an
# unquoted token. The old `"?[^",\s]*"?` stopped at the first comma, so a
# quoted value CONTAINING a comma leaked its tail.
_DIGEST_SECRET_FIELDS = re.compile(
    r'(username|response|nonce|cnonce|opaque|nc)\s*=\s*("(?:[^"\\]|\\.)*"|[^",\s]+)',
    re.IGNORECASE,
)
# User-part of SIP URIs in From/To/Contact headers.
_URI_USER_RE = re.compile(
    r"(sip[s]?:)([^@\s<>;,\"]+)@",
)
# tel: URI numbers (P-Asserted-Identity / Diversion / Remote-Party-ID
# commonly carry the caller's real number as a tel: URI, which the sip:
# masker above never touched).
_TEL_USER_RE = re.compile(r"(tel:)\+?[0-9][0-9\-().]*")
# Quoted display-name in address headers ("Alice Smith" <sip:...>) -- the
# display name is PII (real name / caller-id) and was never redacted.
_DISPLAY_NAME_RE = re.compile(r'"(?:[^"\\]|\\.)*"\s*(?=<)')


def _diagnostic_mode_enabled() -> bool:
    """True iff the user has explicitly opted into raw wire capture."""
    return _os.environ.get("SIP_TRACE_DIAGNOSTIC", "").lower() in (
        "1", "true", "yes", "on",
    )


_redact_cache_val = True
_redact_cache_ts = 0.0
_REDACT_TTL_S = 5.0


def trace_redaction_enabled() -> bool:
    """Return whether SIP trace bodies should be redacted before emit.

    The env var remains as a hard override for support/debug sessions.
    Otherwise use the persisted Settings -> Advanced privacy toggle.
    Default to redaction if settings cannot be read.

    Result is cached for a few seconds: this is called on the PJSIP log
    thread for EVERY SIP message, and load_settings() re-reads/parses the
    settings file from disk -- doing that per packet was a real per-message
    disk hit on the signaling hot path.
    """
    if _diagnostic_mode_enabled():
        return False
    global _redact_cache_val, _redact_cache_ts
    now = time.monotonic()
    if now - _redact_cache_ts < _REDACT_TTL_S:
        return _redact_cache_val
    try:
        from noc_beam.config.store import load_settings

        _redact_cache_val = bool(load_settings().compliance.trace_pii_redaction)
    except Exception:
        _redact_cache_val = True
    _redact_cache_ts = now
    return _redact_cache_val


def redact_sip_body(body: str) -> str:
    """Return a copy of `body` with credentials + user-parts masked.

    - Authorization/Proxy-Authorization/WWW-Authenticate header values
      become "<redacted>"
    - Any leftover digest field (username=, response=, nonce=, ...)
      is replaced with `<field>="<redacted>"`
    - SIP URI user-parts are shortened to first 2 chars + `***`
      ("sip:mada_123@..." -> "sip:ma***@...")
    """
    if not body:
        return body
    out = _AUTH_HEADER_RE.sub(
        lambda m: f"{m.group(0).split(':', 1)[0]}: <redacted>",
        body,
    )
    out = _DIGEST_SECRET_FIELDS.sub(
        lambda m: f'{m.group(1)}="<redacted>"',
        out,
    )
    def _user_mask(m):
        scheme, user = m.group(1), m.group(2)
        if len(user) <= 2:
            masked = "***"
        else:
            masked = user[:2] + "***"
        return f"{scheme}{masked}@"
    out = _URI_USER_RE.sub(_user_mask, out)
    # tel: numbers (PAI/Diversion) and quoted display-names are PII too.
    out = _TEL_USER_RE.sub(lambda m: f"{m.group(1)}<redacted>", out)
    out = _DISPLAY_NAME_RE.sub('"<redacted>" ', out)
    return out
# PJSIP 2.10+ dropped the literal "packet" word in some builds. Match
# both the historical "RX 451 bytes packet from UDP 1.2.3.4:5060"
# format and the newer "RX 451 bytes from UDP 1.2.3.4:5060" form.
_DIR_RX = re.compile(r"\.?RX\s+(\d+)\s+bytes(?:\s+packet)?\s+from\s+(\S+)")
_DIR_TX = re.compile(r"\.?TX\s+(\d+)\s+bytes(?:\s+packet)?\s+to\s+(\S+)")


# pjsua2's LogConfig.writer setter is type-checked at the SWIG layer:
# it requires a pj::LogWriter*. Passing a plain Python class fails with
# "in method 'LogConfig.writer_set', argument 2 of type 'pj::LogWriter *'".
# Fix: when pjsua2 is loaded, inherit from pj.LogWriter so SWIG's
# director mechanism can route C++ callbacks back to our write() method.
# When pjsua2 is unavailable (UI-only stub mode), fall back to a plain
# class that nothing calls into anyway.
if PJSUA2_AVAILABLE and hasattr(pj, "LogWriter"):
    _LogWriterBase = pj.LogWriter
else:
    _LogWriterBase = object


class TraceLogWriter(_LogWriterBase):
    """A pjsua2 LogWriter that emits both raw lines and parsed SIP messages."""

    def __init__(self) -> None:
        # Initialize the SWIG-bound base when present, otherwise no-op.
        if _LogWriterBase is not object:
            _LogWriterBase.__init__(self)
        self._buf: list[str] = []
        self._capturing = False
        self._direction = "?"
        self._peer = "?"

    # pjsua2 expects an object with a `write(self, entry)` method where
    # entry has .msg, .level, .threadName attributes.
    def write(self, entry) -> None:  # noqa: D401, ANN001
        msg = getattr(entry, "msg", str(entry))
        level = getattr(entry, "level", 4)

        for raw_line in msg.splitlines():
            line = raw_line.rstrip()
            sip_events().log_line.emit(level, line)
            self._consume(line)

    def _consume(self, line: str) -> None:
        # Detect direction headers emitted by pjsip just before the SIP body.
        m_rx = _DIR_RX.search(line)
        if m_rx:
            self._flush()
            self._direction = "RX"
            self._peer = m_rx.group(2)
            self._capturing = True
            return
        m_tx = _DIR_TX.search(line)
        if m_tx:
            self._flush()
            self._direction = "TX"
            self._peer = m_tx.group(2)
            self._capturing = True
            return

        # Direction header missing? Fall back to detecting a SIP request
        # or status line directly. PJSIP builds without the canonical
        # "RX/TX N bytes from/to" preamble (or where the preamble was
        # logged at a different level) still emit the message body.
        if not self._capturing and _SIP_START.match(line):
            self._direction = "?"
            self._peer = "?"
            self._capturing = True
            self._buf.append(line)
            return

        if not self._capturing:
            return

        # Capturing: collect until we hit a blank line OR a line that doesn't
        # look like part of a SIP message.
        if line == "":
            self._buf.append(line)
            # blank line could be body separator, keep going for a few lines
            return

        # End of capture heuristic: a new pjsip log header. Pjsip log
        # lines have the shape "HH:MM:SS.mmm  source.tag  ..." -- after
        # the timestamp there's whitespace, then the source category
        # (alphanumerics + dots, e.g. `sip_endpoint.c`). Requiring the
        # source-tag suffix prevents premature flush on SIP body lines
        # that happen to start with a timestamp-shaped string (e.g.
        # a Date header value or an SDP attribute).
        if re.match(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s+\S", line):
            self._flush()
            return

        self._buf.append(line)

    def _flush(self) -> None:
        if not self._buf:
            return
        body = "\n".join(self._buf).strip()
        if body and _SIP_START.search(body):
            # Mask credentials + user-parts before emitting unless the
            # user has explicitly opted into raw diagnostic tracing.
            emit_body = redact_sip_body(body) if trace_redaction_enabled() else body
            sip_events().sip_message.emit(
                time.time(), self._direction, self._peer, emit_body,
            )
        self._buf.clear()
        self._capturing = False


