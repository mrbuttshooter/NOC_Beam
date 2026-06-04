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
# Guards _per_call. Attach + per-call detach now both run on the Qt main
# thread (detach was moved out of the PJSIP-thread onCallState), but
# stop_fas_engine() can iterate during shutdown -- the RLock makes all
# mutations safe and allows attach() -> _detach_internal() reentry.
import threading as _threading
_per_call_lock = _threading.RLock()


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
        _log_model_inventory()


def _log_model_inventory() -> None:
    """Report which FAS models are bundled, loudly flagging a degraded
    build. AASIST is the anti-spoof / false-answer discriminator; without
    it the ensemble is just VAD + audio-event classification and verdicts
    collapse toward INCONCLUSIVE regardless of how much audio is captured
    (field logs showed conf pinned at 0.15 on a 33 s call with >1 MB of
    PCM). This warning makes a degraded build a deliberate, visible
    choice instead of a silent one-line warning buried mid-session.
    """
    try:
        from noc_beam.audio.models import model_path
    except Exception:
        return
    required = {
        "silero_vad.onnx": "voice-activity detection",
        "aasist.onnx": "anti-spoof / false-answer (PRIMARY FAS signal)",
        "Cnn14_16k.onnx": "audio-event classification",
    }
    present, missing = [], []
    for fname, role in required.items():
        try:
            ok = model_path(fname).exists()
        except Exception:
            ok = False
        (present if ok else missing).append((fname, role))
    for fname, role in present:
        log.info("FAS model present: %s (%s)", fname, role)
    if missing:
        names = ", ".join(f for f, _ in missing)
        log.error(
            "FAS DEGRADED MODE: missing model(s) [%s]. Verdicts will be "
            "unreliable. Drop the file(s) into noc_beam/audio/models/ and "
            "rebuild. See build/MODELS.lock for the required model contract.",
            names,
        )
        if any(f == "aasist.onnx" for f, _ in missing):
            log.error(
                "FAS: aasist.onnx absent -> NO anti-spoof signal; FAS "
                "cannot confidently flag false-answer/synthetic audio.",
            )


def stop_fas_engine() -> None:
    """Tear down the FAS engine. Call during endpoint shutdown.

    Order matters: detach all calls before joining the worker so the
    worker's last poll doesn't reference torn-down buffers.
    """
    global _worker_started, _enabled
    with _per_call_lock:
        _open_ids = list(_per_call.keys())
    for call_id in _open_ids:
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
    with _per_call_lock:
        already_attached = call_id in _per_call
    if already_attached:
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

        # Honor the FasSettings.record_clips privacy toggle. retain_on_disk
        # was hardcoded True, so call audio (PII) was written to disk and
        # kept forever even when the operator turned clip retention OFF.
        # When off, FasWavTap still records to a temp WAV (the tap needs a
        # file to tail) but unlinks it on stop().
        retain = True
        try:
            from noc_beam.config.store import load_settings
            retain = bool(load_settings().fas.record_clips)
        except Exception:
            retain = True
        tap = FasWavTap(call_id, call_audio, retain_on_disk=retain)
        fas_router().attach(call_id, **meta)
        if not tap.start():
            log.warning("FAS WAV tap start failed for call %s", call_id)
            fas_router().detach(call_id)
            return
        with _per_call_lock:
            _per_call[call_id] = {"tap": tap, "audio": call_audio}
        fas_worker().track(call_id)
        log.debug("FAS attached to call %s", call_id)
    except Exception:
        fas_router().detach(call_id)
        log.exception("Failed to attach FAS to call %s", call_id)


def detach_fas_from_call(call_id: int) -> None:
    """Tear down a call's tap. Idempotent. Safe in DISCONNECTED handler."""
    _detach_internal(call_id, quiet=False)


def _detach_internal(call_id: int, *, quiet: bool) -> None:
    """Internal detach. When quiet=True (re-attach path), tap-stop
    failures are logged at debug level since the stale handle is
    expected to be partially broken."""
    fas_worker().untrack(call_id)
    with _per_call_lock:
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
