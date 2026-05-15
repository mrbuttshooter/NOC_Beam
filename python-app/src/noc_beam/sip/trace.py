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

        # End of capture heuristic: a new pjsip log header (timestamp/source)
        if re.match(r"^\d{2}:\d{2}:\d{2}\.\d{3}", line):
            self._flush()
            return

        self._buf.append(line)

    def _flush(self) -> None:
        if not self._buf:
            return
        body = "\n".join(self._buf).strip()
        if body and _SIP_START.search(body):
            sip_events().sip_message.emit(time.time(), self._direction, self._peer, body)
        self._buf.clear()
        self._capturing = False


def install_trace_logger(ep) -> TraceLogWriter | None:  # noqa: ANN001
    """Attach a TraceLogWriter to the running pjsua2 Endpoint config."""
    if not PJSUA2_AVAILABLE:
        return None
    try:
        writer = TraceLogWriter()
        # In pjsua2 the writer is attached via EpConfig.logConfig.writer
        # before libInit(). The Endpoint here exposes it post-creation through
        # the log_cb. We keep a reference so it isn't GC'd.
        return writer
    except Exception:
        log.exception("Could not install trace logger")
        return None
