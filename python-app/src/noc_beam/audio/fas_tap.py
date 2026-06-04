"""PJSIP audio tap for FAS detection (AudioMediaRecorder + WAV tail-read).

Replaces the earlier `pj.AudioMediaPort` subclass which silently lost
frames after ~5 deliveries due to SWIG-director / conference-bridge
lifecycle issues. The recorder path is the production-grade pjsua2
pattern used by virtually every call-recording app on GitHub: PJSIP
writes the call's downlink audio into a WAV file we control, and a
background thread tails the file and pushes growing bytes into the
FasAudioRouter for the inference worker to consume.

The WAV files double as the audio clip retention feature
(`FasSettings.record_clips`) so this is "do the work once, use it
twice".

THREADING
- start()/stop() run on Qt main thread (called from sip/call.py).
- The tail-read thread is a daemon threading.Thread (not QThread) --
  no Qt calls from it; the only output is fas_router().push(...).
- PJSIP runs the recorder on its own audio threads; nothing in this
  module is called from there.
"""
from __future__ import annotations

import logging
import os
import struct
import tempfile
import threading
import time
import wave
from pathlib import Path

from noc_beam.config.paths import data_dir
from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE, pj

log = logging.getLogger(__name__)

# Storage format for the FAS audio router. The tap captures at whatever
# rate PJSIP records the call at (typically 16 kHz native bridge rate).
# Worker resamples as needed before model inference.
FAS_SAMPLE_RATE = 16000
FAS_CHANNELS = 1
FAS_BITS_PER_SAMPLE = 16
# Tail-read cadence. 250 ms = 4x per second; fast enough to keep the
# inference schedule (3 s / 8 s / 13 s) snappy; slow enough that the
# OS doesn't sweat the polling.
TAIL_POLL_INTERVAL_S = 0.25

# Recorded WAV header size for canonical 16-bit mono PCM written by PJSIP.
WAV_HEADER_BYTES = 44


def _clip_dir() -> Path:
    """Where to drop per-call WAV clips. Honors FasSettings.record_clips
    retention; if disabled, we still write here and unlink on stop()."""
    d = data_dir() / "fas_clips"
    d.mkdir(parents=True, exist_ok=True)
    return d


class _WavTailReader(threading.Thread):
    """Daemon thread that watches a WAV file grow and pushes raw PCM
    bytes into the FAS router for one specific call.

    Stops cleanly when stop_event is set OR when the file disappears.
    Drains a final read pass on stop to capture any audio that landed
    after the last poll.
    """

    def __init__(
        self,
        call_id: int,
        wav_path: Path,
        stop_event: threading.Event,
        *,
        poll_interval_s: float = TAIL_POLL_INTERVAL_S,
    ) -> None:
        super().__init__(daemon=True, name=f"FasWavTail-{call_id}")
        self.call_id = call_id
        self.wav_path = wav_path
        self._stop = stop_event
        self._poll = poll_interval_s
        self._offset = WAV_HEADER_BYTES
        self._carry = b""  # half-frame remainder between polls
        self.frames_pushed = 0
        self.bytes_pushed = 0

    def run(self) -> None:  # noqa: D401
        # Wait for PJSIP to actually create the file before opening.
        deadline = time.monotonic() + 5.0
        while not self.wav_path.exists() and time.monotonic() < deadline:
            if self._stop.is_set():
                return
            time.sleep(0.05)
        if not self.wav_path.exists():
            log.warning("FAS WAV tail: file never appeared at %s", self.wav_path)
            return
        log.info("FAS WAV tail started call=%s path=%s", self.call_id, self.wav_path)
        try:
            while not self._stop.is_set():
                self._drain_once()
                # Sleep in small ticks so stop_event responsiveness is good.
                slept = 0.0
                while slept < self._poll and not self._stop.is_set():
                    time.sleep(0.05)
                    slept += 0.05
            # Final drain after stop to catch any trailing audio.
            self._drain_once()
        except Exception:
            log.exception("FAS WAV tail error call=%s", self.call_id)
        finally:
            log.info(
                "FAS WAV tail stopped call=%s frames_pushed=%d bytes_pushed=%d",
                self.call_id, self.frames_pushed, self.bytes_pushed,
            )

    def _drain_once(self) -> None:
        try:
            size = self.wav_path.stat().st_size
        except FileNotFoundError:
            self._stop.set()
            return
        if size <= self._offset:
            return
        new_bytes = size - self._offset
        try:
            with self.wav_path.open("rb") as f:
                f.seek(self._offset)
                data = f.read(new_bytes)
        except Exception:
            return
        if not data:
            return
        # Frame-align to 2 bytes (int16 mono). Keep any odd byte for
        # the next poll instead of losing it.
        buf = self._carry + data
        odd = len(buf) & 1
        if odd:
            self._carry = buf[-1:]
            buf = buf[:-1]
        else:
            self._carry = b""
        if not buf:
            self._offset = size
            return
        # Lazy import so this module doesn't pull in router at import
        # time (tap is imported during boot before router init).
        from noc_beam.audio.fas_router import fas_router

        fas_router().push(self.call_id, buf)
        self.frames_pushed += 1
        self.bytes_pushed += len(buf)
        # Advance past everything we read from the file. The odd trailing
        # byte (if any) is held in self._carry IN MEMORY and prepended on
        # the next poll -- it must NOT be re-read from disk. The old
        # `size - len(self._carry)` rewound the offset onto the carried
        # byte, so the next read pulled it from the file too, duplicating
        # it and byte-swapping every following int16 sample (a noise burst
        # into the inference window).
        self._offset = size


class FasWavTap:
    """Records a call's downlink audio to a WAV and pushes growing
    bytes into the FAS router. Constructor is cheap; start() actually
    creates the recorder and begins capturing."""

    def __init__(self, call_id: int, call_audio, *, retain_on_disk: bool = True) -> None:
        self.call_id = call_id
        self._call_audio = call_audio
        self._retain = retain_on_disk
        self._recorder = None
        self._reader: _WavTailReader | None = None
        self._stop_event = threading.Event()
        # File is created by PJSIP. We just pick a unique path.
        ts = int(time.time())
        self.wav_path = _clip_dir() / f"call-{call_id}-{ts}.wav"
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> bool:
        if self._started:
            return True
        if not PJSUA2_AVAILABLE:
            log.warning("FasWavTap: pjsua2 not available")
            return False
        # Build the tail-reader FIRST so a disconnect that fires between
        # startTransmit() and _started=True still gets cleaned up via stop().
        # Previously, if the call disconnected in that window, stop() saw
        # _started==False and returned early, leaking the recorder + bridge
        # port. We set _started=True before any I/O so stop() always runs.
        self._reader = _WavTailReader(self.call_id, self.wav_path, self._stop_event)
        self._started = True
        try:
            self._recorder = pj.AudioMediaRecorder()
            self._recorder.createRecorder(str(self.wav_path))
            self._call_audio.startTransmit(self._recorder)
            self._reader.start()
        except Exception:
            log.exception("FasWavTap start failed for call %s", self.call_id)
            # Roll back the optimistic state and let stop() finish cleanup
            # via the normal path.
            try:
                self.stop()
            except Exception:
                log.exception("FasWavTap rollback stop() raised")
            self._started = False
            return False
        log.info("FasWavTap started call=%s wav=%s", self.call_id, self.wav_path)
        return True

    def stop(self) -> None:
        if not self._started:
            return
        # Signal tail-reader to drain + exit BEFORE we tear down the
        # recorder so we don't miss the last few hundred ms. Each
        # sub-step is wrapped independently so a benign hiccup on
        # stopTransmit doesn't block recorder cleanup, and a recorder
        # double-delete doesn't block the reader join.
        try:
            self._stop_event.set()
        except Exception:
            log.debug("stop_event set raised call=%s", self.call_id, exc_info=True)
        try:
            if self._call_audio is not None and self._recorder is not None:
                self._call_audio.stopTransmit(self._recorder)
        except Exception:
            log.debug("stopTransmit raised on call %s", self.call_id, exc_info=True)
        try:
            if self._recorder is not None:
                # AudioMediaRecorder destructor finalizes the WAV header.
                del self._recorder
                self._recorder = None
        except Exception:
            log.debug("recorder del raised on call %s", self.call_id, exc_info=True)
        try:
            if self._reader is not None:
                self._reader.join(timeout=2.0)
                self._reader = None
        except Exception:
            log.debug("reader join raised on call %s", self.call_id, exc_info=True)
        self._started = False
        if not self._retain:
            try:
                self.wav_path.unlink(missing_ok=True)
            except OSError:
                log.debug("Could not unlink %s", self.wav_path, exc_info=True)
        log.info("FasWavTap stopped call=%s", self.call_id)
