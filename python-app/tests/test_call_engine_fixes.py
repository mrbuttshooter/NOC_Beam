"""Regression tests for call-engine bugs found on the native engine (2026-09).

Each bug was reproduced with an in-process loopback call on the bundled
pjsua2 before it was fixed; these tests pin the fixed behaviour without a
live endpoint by invoking the unbound SipEndpoint methods on stubs.

  * resume_call used setHold(), which in PJSIP always re-holds: a second
    `a=sendonly` re-INVITE went out and the call stayed held.
  * hangup_call sent 603 Decline for a ringing INCOMING call (EARLY after
    our 180 shares the state number of an outbound early dialog).
  * pjsua2.Error stringified to "" -- logs and the "Call failed" dialog
    carried no reason.
  * the "inband" DTMF setting sent RFC 2833 events, never audio tones.
"""
from __future__ import annotations

import types

import pytest

from noc_beam.sip import endpoint as endpoint_module
from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE, _pj_error_str
from noc_beam.sip.endpoint import SipEndpoint

needs_pjsua2 = pytest.mark.skipif(not PJSUA2_AVAILABLE, reason="pjsua2 not loadable")


# --------------------------------------------------------------------------
# resume
# --------------------------------------------------------------------------
class _HoldCall:
    def __init__(self) -> None:
        self.ops: list[tuple[str, int]] = []

    def setHold(self, prm) -> None:  # noqa: N802 (pjsua2 name)
        self.ops.append(("setHold", int(prm.opt.flag)))

    def reinvite(self, prm) -> None:
        self.ops.append(("reinvite", int(prm.opt.flag)))


@needs_pjsua2
def test_resume_sends_unhold_reinvite_not_another_hold() -> None:
    call = _HoldCall()
    SipEndpoint.resume_call(types.SimpleNamespace(), call)
    unhold = int(getattr(endpoint_module.pj, "PJSUA_CALL_UNHOLD", 1))
    assert call.ops == [("reinvite", unhold)]


@needs_pjsua2
def test_resume_failure_propagates_so_ui_stays_held() -> None:
    class _Busy(_HoldCall):
        def reinvite(self, prm) -> None:
            raise RuntimeError("another INVITE transaction in progress")

    with pytest.raises(RuntimeError):
        SipEndpoint.resume_call(types.SimpleNamespace(), _Busy())


# --------------------------------------------------------------------------
# hangup
# --------------------------------------------------------------------------
class _HangupCall:
    def __init__(self, *, state: int, role: int) -> None:
        self._info = types.SimpleNamespace(state=state, role=role)
        self.codes: list[int] = []

    def getInfo(self):  # noqa: N802
        return self._info

    def hangup(self, prm) -> None:
        self.codes.append(int(prm.statusCode))


@needs_pjsua2
@pytest.mark.parametrize("state", [2, 3])  # INCOMING, EARLY (after our 180)
def test_hangup_of_ringing_incoming_call_sends_486(state: int) -> None:
    call = _HangupCall(state=state, role=1)  # UAS
    SipEndpoint.hangup_call(types.SimpleNamespace(), call)
    assert call.codes == [486]


@needs_pjsua2
@pytest.mark.parametrize("state", [1, 3])  # CALLING, EARLY
def test_hangup_of_outbound_early_call_still_cancels(state: int) -> None:
    call = _HangupCall(state=state, role=0)  # UAC
    SipEndpoint.hangup_call(types.SimpleNamespace(), call)
    assert call.codes == [0]  # statusCode 0 -> PJSIP sends CANCEL


@needs_pjsua2
def test_explicit_reject_code_wins() -> None:
    call = _HangupCall(state=3, role=1)
    SipEndpoint.hangup_call(types.SimpleNamespace(), call, code=603)
    assert call.codes == [603]


# --------------------------------------------------------------------------
# readable pjsua2 errors
# --------------------------------------------------------------------------
def test_pj_error_str_renders_reason_function_and_status() -> None:
    err = types.SimpleNamespace(
        reason="INVITE session already terminated (PJSIP_ESESSIONTERMINATED)",
        title="pjsua_call_answer2(id, param.p_opt, prm.statusCode, ...)",
        status=171140,
    )
    assert _pj_error_str(err) == (
        "INVITE session already terminated (PJSIP_ESESSIONTERMINATED) "
        "[pjsua_call_answer2, status 171140]"
    )


def test_pj_error_str_never_empty() -> None:
    assert _pj_error_str(types.SimpleNamespace(reason="", title="", status=0)) == "pjsua2 error"


@needs_pjsua2
def test_real_pjsua2_error_class_uses_readable_str() -> None:
    err_cls = endpoint_module.pj.Error
    assert err_cls.__str__ is _pj_error_str


# --------------------------------------------------------------------------
# in-band DTMF
# --------------------------------------------------------------------------
class _FakeToneGen:
    instances: list["_FakeToneGen"] = []

    def __init__(self) -> None:
        self.created = False
        self.targets: list[object] = []
        self.played: list[tuple[str, int, int]] = []
        _FakeToneGen.instances.append(self)

    def createToneGenerator(self, *a) -> None:  # noqa: N802
        self.created = True

    def startTransmit(self, sink) -> None:  # noqa: N802
        self.targets.append(sink)

    def playDigits(self, seq) -> None:  # noqa: N802
        self.played.extend((str(d.digit), int(d.on_msec), int(d.off_msec)) for d in seq)


class _MediaCall:
    def __init__(self) -> None:
        self.audio = object()
        self.dialed: list[str] = []

    def getInfo(self):  # noqa: N802
        media = [types.SimpleNamespace(type=1, status=1, index=0)]
        return types.SimpleNamespace(media=media)

    def getAudioMedia(self, idx):  # noqa: N802
        return self.audio

    def dialDtmf(self, digits) -> None:  # noqa: N802
        self.dialed.append(digits)


def _inband_self():
    s = types.SimpleNamespace(
        _ensure_thread_registered=lambda: None,
        _conf_clock_rate=16000,
        _INBAND_ON_MS=SipEndpoint._INBAND_ON_MS,
        _INBAND_OFF_MS=SipEndpoint._INBAND_OFF_MS,
    )
    s._active_audio_media = SipEndpoint._active_audio_media
    s._ensure_call_clock = lambda call, aud: SipEndpoint._ensure_call_clock(s, call, aud)
    return s


@needs_pjsua2
def test_inband_plays_tones_into_call_audio(monkeypatch) -> None:
    _FakeToneGen.instances.clear()
    monkeypatch.setattr(endpoint_module.pj, "ToneGenerator", _FakeToneGen)
    call = _MediaCall()
    SipEndpoint._send_dtmf_inband(_inband_self(), call, "15#")
    (gen,) = _FakeToneGen.instances
    assert gen.created and gen.targets == [call.audio]
    assert [d for d, _, _ in gen.played] == ["1", "5", "#"]
    assert call.dialed == []  # never RFC 2833


@needs_pjsua2
def test_inband_reuses_one_generator_per_call(monkeypatch) -> None:
    _FakeToneGen.instances.clear()
    monkeypatch.setattr(endpoint_module.pj, "ToneGenerator", _FakeToneGen)
    call = _MediaCall()
    s = _inband_self()
    SipEndpoint._send_dtmf_inband(s, call, "1")
    SipEndpoint._send_dtmf_inband(s, call, "2")
    (gen,) = _FakeToneGen.instances
    assert [d for d, _, _ in gen.played] == ["1", "2"]
    # re-attached every burst: a re-INVITE can rebuild the call's conf slot
    assert gen.targets == [call.audio, call.audio]


def test_send_dtmf_routes_inband_away_from_rfc2833() -> None:
    s = types.SimpleNamespace(inband=[], info=[])
    s._send_dtmf_inband = lambda call, digits: s.inband.append(digits)
    s._send_dtmf_info = lambda call, digits: s.info.append(digits)
    call = _MediaCall()
    SipEndpoint.send_dtmf(s, call, "9", types.SimpleNamespace(dtmf_method="inband"))
    assert s.inband == ["9"]
    assert call.dialed == [] and s.info == []


# --------------------------------------------------------------------------
# per-call media clock (unfocused calls must still be serviced)
# --------------------------------------------------------------------------
class _Port:
    def __init__(self, name: str, log: list) -> None:
        self.name, self.log = name, log

    def startTransmit(self, sink) -> None:  # noqa: N802
        self.log.append(("start", self.name, getattr(sink, "name", sink)))

    def stopTransmit(self, sink) -> None:  # noqa: N802
        self.log.append(("stop", self.name, getattr(sink, "name", sink)))


class _FocusCall:
    def __init__(self, cid: int, log: list) -> None:
        self.cid, self.port = cid, _Port(f"call{cid}", log)

    def getInfo(self):  # noqa: N802
        media = [types.SimpleNamespace(type=1, status=1, index=0)]
        return types.SimpleNamespace(id=self.cid, media=media)

    def getAudioMedia(self, idx):  # noqa: N802
        return self.port


def test_audio_focus_attaches_media_clock_to_every_call() -> None:
    import threading

    wire: list = []
    capture, playback = _Port("mic", wire), _Port("speaker", wire)
    dev = types.SimpleNamespace(getCaptureDevMedia=lambda: capture,
                                getPlaybackDevMedia=lambda: playback)
    focused, other = _FocusCall(0, wire), _FocusCall(1, wire)
    clocked: list[int] = []
    s = types.SimpleNamespace(
        _ep=types.SimpleNamespace(audDevManager=lambda: dev),
        _lock=threading.RLock(),
        _accounts={"acc": types.SimpleNamespace(calls=[focused, other])},
        _muted_call_ids=set(),
        _ensure_thread_registered=lambda: None,
        _ensure_call_clock=lambda call, aud: clocked.append(call.cid),
    )
    SipEndpoint.set_call_audio_focus(s, 0)
    assert sorted(clocked) == [0, 1]          # the unfocused call is clocked too
    assert ("start", "mic", "call0") in wire  # focus routing itself unchanged
    assert ("stop", "mic", "call1") in wire
    assert ("stop", "call1", "speaker") in wire


@needs_pjsua2
def test_media_clock_is_one_idle_generator_per_call(monkeypatch) -> None:
    _FakeToneGen.instances.clear()
    monkeypatch.setattr(endpoint_module.pj, "ToneGenerator", _FakeToneGen)
    s = _inband_self()
    call = _MediaCall()
    first = SipEndpoint._ensure_call_clock(s, call, call.audio)
    second = SipEndpoint._ensure_call_clock(s, call, call.audio)
    assert first is second and len(_FakeToneGen.instances) == 1
    assert first.played == []                 # idle: silence only
    assert first.targets == [call.audio, call.audio]
