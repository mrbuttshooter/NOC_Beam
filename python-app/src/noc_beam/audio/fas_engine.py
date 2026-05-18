"""Public entry point for FAS detection.

Coordinates the tap port (per-call), the audio router (shared), and the
inference worker (singleton). One process-wide on/off kill switch.

Lifecycle:
    start_fas_engine()            -> launch worker thread
    attach_fas_to_call(call_id)   -> create + bind tap, register with router,
                                     start scoring schedule
    detach_fas_from_call(call_id) -> stop scoring, drop tap + buffer
    stop_fas_engine()             -> join worker, clear router

If FAS detection is disabled in settings, attach_fas_to_call returns
without doing anything -- safe to call unconditionally from SIP callbacks.

This module is import-safe even when onnxruntime / pjsua2 are missing;
calls into it become no-ops with a log line.
"""
from __future__ import annotations

import logging
from typing import Any

from noc_beam.audio.fas_router import fas_router
from noc_beam.audio.fas_worker import fas_worker, shutdown_fas_worker
from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE

log = logging.getLogger(__name__)

_enabled = False
_worker_started = False
# call_id -> {"tap": FasWavTap, "audio": pj.AudioMedia | None}
_per_call: dict[int, dict[str, Any]] = {}


def is_enabled() -> bool:
    return _enabled


def start_fas_engine(enabled: bool = True) -> None:
    """Initialise the FAS engine. Safe to call repeatedly."""
    global _enabled, _worker_started
    _enabled = bool(enabled)
    if not _enabled:
        log.info("FAS engine disabled by settings")
        return
    if not PJSUA2_AVAILABLE:
        log.warning("FAS engine: pjsua2 not available; tap will be skipped")
    if not _worker_started:
        w = fas_worker()
        w.start()
        _worker_started = True
        log.info("FAS engine started")


def stop_fas_engine() -> None:
    """Tear down the FAS engine. Call during endpoint shutdown.

    Order matters: detach all calls before joining the worker so the
    worker's last poll doesn't reference torn-down buffers.
    """
    global _worker_started, _enabled
    for call_id in list(_per_call.keys()):
        detach_fas_from_call(call_id)
    fas_router().teardown()
    if _worker_started:
        shutdown_fas_worker()
        _worker_started = False
    # Release ONNX InferenceSession refs held by module-level singletons
    # (_silero / _aasist / _panns). Without this, restarting the worker
    # in the same Python interpreter (test runs, hot reload) leaks the
    # native sessions. Best-effort -- if fas_models lacks the symbol
    # (older build) we just skip.
    try:
        from noc_beam.audio.fas_models import shutdown_models
        shutdown_models()
    except Exception:
        pass
    _enabled = False


def attach_fas_to_call(call_id: int, call_audio: Any, **meta: Any) -> None:
    """Attach a tap to a CONFIRMED call's audio media.

    `call_audio` is the pjsua2.AudioMedia returned by call.getAudioMedia(mi.index).

    onCallMediaState fires multiple times during call setup (initial media,
    codec lock, re-INVITE, hold/unhold). Each call brings a fresh AudioMedia
    proxy bound to the live conf-bridge slot; older proxies become stale
    wrappers whose conf-bridge connections die silently. If we skip
    re-attach when call_id is already known, the tap stays bound to the
    first (now-defunct) media handle and audio stops flowing after the
    first ~5 frames. So: ALWAYS tear down and re-attach with the new
    media handle.
    """
    if not _enabled:
        return
    if not PJSUA2_AVAILABLE:
        return
    if call_id in _per_call:
        # Re-attach with the new media handle. Don't return early.
        log.info("FAS re-attach call=%s (onCallMediaState fired again)", call_id)
        _detach_internal(call_id, quiet=True)
    try:
        # Use AudioMediaRecorder + WAV tail-read instead of an
        # AudioMediaPort subclass. The recorder is the battle-tested
        # PJSIP path; the subclass path silently dropped frames after
        # ~5 deliveries due to SWIG-director / conference-bridge
        # lifecycle issues that exhausted four attempts to fix.
        from noc_beam.audio.fas_tap import FasWavTap

        tap = FasWavTap(call_id, call_audio, retain_on_disk=True)
        if not tap.start():
            log.warning("FAS WAV tap start failed for call %s", call_id)
            return
        _per_call[call_id] = {"tap": tap, "audio": call_audio}
        fas_router().attach(call_id, **meta)
        fas_worker().track(call_id)
        log.debug("FAS attached to call %s", call_id)
    except Exception:
        log.exception("Failed to attach FAS to call %s", call_id)


def detach_fas_from_call(call_id: int) -> None:
    """Tear down a call's tap. Idempotent. Safe in DISCONNECTED handler."""
    _detach_internal(call_id, quiet=False)


def _detach_internal(call_id: int, *, quiet: bool) -> None:
    """Internal detach. When quiet=True (re-attach path), tap-stop
    failures are logged at debug level since the stale handle is
    expected to be partially broken."""
    fas_worker().untrack(call_id)
    entry = _per_call.pop(call_id, None)
    if entry:
        tap = entry.get("tap")
        try:
            if tap is not None:
                tap.stop()
        except Exception:
            if quiet:
                log.debug("FAS tap stop (re-attach) had a benign hiccup on call %s",
                          call_id, exc_info=True)
            else:
                log.exception("FAS tap stop raised on call %s", call_id)
    fas_router().detach(call_id)
