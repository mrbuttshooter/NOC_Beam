r"""Loopback check for the NOC_Beam call engine on the REAL native pjsua2.

Places real calls between two local accounts on 127.0.0.1 (null audio
device, threadCnt=0, every callback polled on this thread so each scenario
is deterministic) and drives them through the app's own SipEndpoint methods.
Each scenario checks what actually happened on the engine / wire: media
hold state on both legs, SDP direction sent, final SIP codes, DTMF digits
received, and in-band tones decoded from the far end's recorded audio.

Written while fixing the 2026-09 call-engine bugs (resume re-holding, 603 on
a ringing incoming hangup, silent in-band DTMF, unreadable pjsua2 errors,
SIP trace direction). Run it after any change to sip/endpoint.py:

    cd python-app
    .venv\Scripts\python.exe tools\engine_loopback_check.py          # all
    .venv\Scripts\python.exe tools\engine_loopback_check.py hold_resume

Exit code is the number of failing scenarios. Port: HARNESS_PORT (5098).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from noc_beam.sip._pjsua2_loader import PJSUA2_AVAILABLE, PJSUA2_SOURCE, pj  # noqa: E402

assert PJSUA2_AVAILABLE, "native pjsua2 not loadable"
PORT = int(os.environ.get("HARNESS_PORT", "5098"))
HOST = "127.0.0.1"
MEDIA = {0: "NONE", 1: "ACTIVE", 2: "LOCAL_HOLD", 3: "REMOTE_HOLD", 4: "ERROR"}
STATE = {0: "NULL", 1: "CALLING", 2: "INCOMING", 3: "EARLY", 4: "CONNECTING", 5: "CONFIRMED", 6: "DISCONNECTED"}


class Writer(pj.LogWriter):
    def __init__(self):
        super().__init__()
        self.lines = []

    def write(self, entry):
        self.lines.append(entry.msg)


class HCall(pj.Call):
    def __init__(self, acc, call_id=pj.PJSUA_INVALID_ID, tag=""):
        super().__init__(acc, call_id)
        self.tag = tag
        self.states = []

    def onCallState(self, prm):
        try:
            info = self.getInfo()
            self.states.append(STATE.get(info.state))
            self.last_code = info.lastStatusCode
        except Exception:
            pass

    def onDtmfDigit(self, prm):
        self.digits = getattr(self, "digits", "") + prm.digit


class HAccount(pj.Account):
    def __init__(self, auto_answer=True):
        super().__init__()
        self.incoming = []
        self.calls = []          # SipAccount-compatible, for SipEndpoint focus/mute walks
        self.auto_answer = auto_answer

    def onIncomingCall(self, prm):
        c = HCall(self, prm.callId, tag="callee")
        self.incoming.append(c)
        op = pj.CallOpParam(True)
        op.statusCode = 180
        c.answer(op)
        if self.auto_answer:
            op = pj.CallOpParam(True)
            op.statusCode = 200
            c.answer(op)


class Rig:
    def __init__(self):
        self.ep = pj.Endpoint()
        self.ep.libCreate()
        cfg = pj.EpConfig()
        cfg.uaConfig.threadCnt = 0
        cfg.uaConfig.mainThreadOnly = True
        cfg.uaConfig.maxCalls = 32
        self.writer = Writer()
        cfg.logConfig.writer = self.writer
        cfg.logConfig.level = 5
        cfg.logConfig.consoleLevel = 5
        cfg.logConfig.msgLogging = True
        self.ep.libInit(cfg)
        self.ep.audDevManager().setNullDev()
        tcfg = pj.TransportConfig()
        tcfg.port = PORT
        tcfg.boundAddress = HOST
        self.ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tcfg)
        self.ep.libStart()
        self.caller = self._acc("caller", auto_answer=True)
        self.callee = self._acc("callee", auto_answer=True)

    def _acc(self, user, auto_answer):
        a = HAccount(auto_answer=auto_answer)
        c = pj.AccountConfig()
        c.idUri = f"sip:{user}@{HOST}:{PORT}"
        a.create(c)
        return a

    def pump(self, secs=0.5):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            self.ep.libHandleEvents(10)

    def until(self, pred, secs=5.0):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            self.ep.libHandleEvents(10)
            if pred():
                return True
        return False

    def dial(self, user="callee"):
        c = HCall(self.caller, tag="caller")
        c.makeCall(f"sip:{user}@{HOST}:{PORT}", pj.CallOpParam(True))
        return c

    @staticmethod
    def media(call):
        try:
            info = call.getInfo()
            return ",".join(MEDIA.get(m.status, str(m.status)) for m in info.media if m.type == 1) or "-"
        except Exception as e:  # noqa: BLE001
            return f"<{type(e).__name__}>"

    def sdp_dirs_since(self, mark):
        """SDP direction attribute of every INVITE this endpoint sent after `mark`."""
        out = []
        for entry in self.writer.lines[mark:]:
            if "TX " in entry and "Request msg INVITE" in entry:
                for d in ("sendrecv", "sendonly", "recvonly", "inactive"):
                    if f"a={d}" in entry:
                        out.append(d)
                        break
        return out

    def close(self):
        """Drop every pjsua2 object BEFORE libDestroy: a Call / Account /
        ToneGenerator whose C++ destructor runs after the library is gone
        (e.g. during interpreter exit) crashes the process."""
        import gc
        try:
            self.ep.hangupAllCalls()
            self.pump(0.3)
        except Exception:
            pass
        for acc in (self.caller, self.callee):
            acc.incoming.clear()
            acc.calls.clear()
            try:
                acc.shutdown()
            except Exception:
                pass
        self.caller = self.callee = None
        gc.collect()
        self.pump(0.2)
        self.ep.libDestroy()


def connected_pair(rig):
    a = rig.dial()
    ok = rig.until(lambda: rig.callee.incoming and "CONFIRMED" in a.states
                   and "CONFIRMED" in rig.callee.incoming[-1].states)
    assert ok, f"call never connected: caller={a.states}"
    rig.pump(0.3)
    return a, rig.callee.incoming[-1]


# --------------------------------------------------------------------- scenarios
def scenario_hold_resume(rig):
    """Does the app's resume_call actually take the far end off hold?"""
    from noc_beam.sip.endpoint import SipEndpoint

    ep = SipEndpoint.instance()
    a, b = connected_pair(rig)
    print(f"  connected      caller={rig.media(a):<12} callee={rig.media(b)}")
    mark = len(rig.writer.lines)
    ep.hold_call(a)
    rig.pump(1.0)
    print(f"  after hold     caller={rig.media(a):<12} callee={rig.media(b)}  SDP sent: {rig.sdp_dirs_since(mark)}")
    mark = len(rig.writer.lines)
    err = ""
    try:
        ep.resume_call(a)
    except Exception as e:  # noqa: BLE001
        err = f" raised {type(e).__name__}"
    rig.pump(1.0)
    print(f"  after resume   caller={rig.media(a):<12} callee={rig.media(b)}  SDP sent: {rig.sdp_dirs_since(mark)}{err}")
    resumed = rig.media(a) == "ACTIVE" and rig.media(b) == "ACTIVE"
    ep.hangup_call(a)
    rig.pump(0.5)
    return resumed


def scenario_resume_via_reinvite(rig):
    """Reference: PJSIP's documented unhold (reinvite + PJSUA_CALL_UNHOLD)."""
    a, b = connected_pair(rig)
    a.setHold(pj.CallOpParam(True))
    rig.pump(1.0)
    print(f"  after hold     caller={rig.media(a):<12} callee={rig.media(b)}")
    mark = len(rig.writer.lines)
    prm = pj.CallOpParam(True)
    prm.opt.flag = pj.PJSUA_CALL_UNHOLD
    a.reinvite(prm)
    rig.pump(1.0)
    print(f"  after unhold   caller={rig.media(a):<12} callee={rig.media(b)}  SDP sent: {rig.sdp_dirs_since(mark)}")
    ok = rig.media(a) == "ACTIVE" and rig.media(b) == "ACTIVE"
    a.hangup(pj.CallOpParam(True))
    rig.pump(0.5)
    return ok


class _Cfg:
    def __init__(self, method):
        self.dtmf_method = method


def ringing_pair(rig):
    rig.callee.auto_answer = False
    a = rig.dial()
    ok = rig.until(lambda: rig.callee.incoming and "EARLY" in a.states)
    rig.callee.auto_answer = True
    assert ok, f"never rang: {a.states}"
    return a, rig.callee.incoming[-1]


def scenario_cancel_while_ringing(rig):
    from noc_beam.sip.endpoint import SipEndpoint
    a, b = ringing_pair(rig)
    SipEndpoint.instance().hangup_call(a)
    rig.until(lambda: "DISCONNECTED" in b.states)
    print(f"  callee saw {b.states[-1]} code={getattr(b, 'last_code', '?')}")
    return "DISCONNECTED" in b.states and getattr(b, "last_code", 0) == 487


def scenario_reject_603(rig):
    from noc_beam.sip.endpoint import SipEndpoint
    a, b = ringing_pair(rig)
    SipEndpoint.instance().hangup_call(b, code=603)
    rig.until(lambda: "DISCONNECTED" in a.states)
    print(f"  caller got code={getattr(a, 'last_code', '?')}")
    return getattr(a, "last_code", 0) == 603


def scenario_incoming_default_hangup(rig):
    from noc_beam.sip.endpoint import SipEndpoint
    a, b = ringing_pair(rig)
    SipEndpoint.instance().hangup_call(b)
    rig.until(lambda: "DISCONNECTED" in a.states)
    print(f"  caller got code={getattr(a, 'last_code', '?')}")
    return getattr(a, "last_code", 0) == 486


def _dtmf(rig, method):
    from noc_beam.sip.endpoint import SipEndpoint
    a, b = connected_pair(rig)
    for d in "159#":  # one digit per keypress, like the app's keypad
        SipEndpoint.instance().send_dtmf(a, d, _Cfg(method))
        rig.pump(0.4)
    rig.until(lambda: getattr(b, "digits", "") == "159#", 3.0)
    got = getattr(b, "digits", "")
    print(f"  method={method} callee received {got!r}")
    a.hangup(pj.CallOpParam(True)); rig.pump(0.4)
    return got == "159#"


def scenario_dtmf_rfc2833(rig):
    return _dtmf(rig, "rfc2833")


def scenario_dtmf_info(rig):
    return _dtmf(rig, "info")


def scenario_pj_error_detail(rig):
    """What does a pjsua2.Error carry, and what does str() show?"""
    a, b = connected_pair(rig)
    a.hangup(pj.CallOpParam(True)); rig.pump(0.4)
    try:
        prm = pj.CallOpParam(True); prm.statusCode = 200
        b.answer(prm)
        print("  no error raised"); return False
    except Exception as e:  # noqa: BLE001
        attrs = {k: getattr(e, k, None) for k in ("status", "title", "reason", "srcFile", "srcLine")}
        print(f"  type={type(e).__name__} str={str(e)!r} repr={repr(e)[:60]!r}")
        print(f"  attrs={attrs}")
        info = getattr(e, "info", None)
        print(f"  info()={info() if callable(info) else None!r}")
        return bool(str(e))


def scenario_hold_resume_cycles(rig):
    from noc_beam.sip.endpoint import SipEndpoint
    ep = SipEndpoint.instance()
    a, b = connected_pair(rig)
    ok = True
    for i in range(3):
        ep.hold_call(a); rig.pump(0.8)
        held = (rig.media(a), rig.media(b))
        ep.resume_call(a); rig.pump(0.8)
        live = (rig.media(a), rig.media(b))
        print(f"  cycle {i+1}: hold -> {held}  resume -> {live}")
        ok = ok and held == ("LOCAL_HOLD", "REMOTE_HOLD") and live == ("ACTIVE", "ACTIVE")
    a.hangup(pj.CallOpParam(True)); rig.pump(0.4)
    return ok


def _decode_dtmf_wav(path):
    """Goertzel DTMF decoder over a mono 16-bit WAV. Returns the digit string."""
    import math, struct, wave
    with wave.open(path, "rb") as w:
        rate = w.getframerate(); n = w.getnframes(); ch = w.getnchannels()
        raw = w.readframes(n)
    samples = struct.unpack("<%dh" % (len(raw) // 2), raw)[::ch]
    lows, highs = [697, 770, 852, 941], [1209, 1336, 1477, 1633]
    keys = ["123A", "456B", "789C", "*0#D"]
    win = int(rate * 0.03)
    def power(block, f):
        k = 2 * math.cos(2 * math.pi * f / rate); s1 = s2 = 0.0
        for x in block:
            s1, s2 = x + k * s1 - s2, s1
        return s1 * s1 + s2 * s2 - k * s1 * s2
    out, last = [], None
    for i in range(0, len(samples) - win, win):
        block = samples[i:i + win]
        energy = sum(x * x for x in block) / win
        if energy < 1e4:
            last = None; continue
        pl = [power(block, f) for f in lows]; ph = [power(block, f) for f in highs]
        li, hi = pl.index(max(pl)), ph.index(max(ph))
        total = sum(pl) + sum(ph)
        if total and (pl[li] + ph[hi]) / total > 0.6:
            d = keys[li][hi]
            if d != last:
                out.append(d)
            last = d
        else:
            last = None
    return "".join(out)


def scenario_dtmf_inband(rig):
    """Does 'inband' put real DTMF tones in the audio the far end hears?"""
    import tempfile
    from noc_beam.sip.endpoint import SipEndpoint
    a, b = connected_pair(rig)
    wav = os.path.join(tempfile.gettempdir(), "nocbeam_inband_rx.wav")
    rec = pj.AudioMediaRecorder(); rec.createRecorder(wav)
    b_aud = b.getAudioMedia(-1)
    b_aud.startTransmit(rec)
    SipEndpoint.instance().send_dtmf(a, "159#", _Cfg("inband"))
    rig.pump(2.5)
    b_aud.stopTransmit(rec); del rec; rig.pump(0.2)
    tones = _decode_dtmf_wav(wav)
    events = getattr(b, "digits", "")
    print(f"  tones heard in far-end audio: {tones!r}   RFC2833 events received: {events!r}")
    a.hangup(pj.CallOpParam(True)); rig.pump(0.4)
    return tones == "159#" and events == ""


class _bound_endpoint:
    """Temporarily point the app's SipEndpoint singleton at this rig so its
    real set_call_audio_focus / set_call_mute / send_dtmf code runs."""
    def __init__(self, rig):
        from noc_beam.sip.endpoint import SipEndpoint
        self.ep, self.rig = SipEndpoint.instance(), rig
    def __enter__(self):
        self.saved = (self.ep._ep, self.ep._accounts)
        self.ep._ep = self.rig.ep
        self.ep._accounts = {"caller": self.rig.caller}
        return self.ep
    def __exit__(self, *exc):
        self.ep._ep, self.ep._accounts = self.saved
        self.rig.caller.calls.clear()


def _far_end_rtp_rate(rig, b, secs=2.0):
    def pkts():
        try:
            return b.getStreamStat(0).rtcp.rxStat.pkt
        except Exception:
            return 0
    p0 = pkts(); rig.pump(secs)
    return round((pkts() - p0) / secs, 1)


def _keypad(rig, ep, a, digits="159#"):
    for d in digits:  # one digit per keypress, like the app's keypad
        ep.send_dtmf(a, d, _Cfg("rfc2833"))
        rig.pump(0.4)
    rig.pump(1.5)


def scenario_unfocused_call(rig):
    """A call without audio focus (non-selected card, every test-runner call)
    must still send RTP and deliver every keypad digit."""
    with _bound_endpoint(rig) as ep:
        a, b = connected_pair(rig)
        rig.caller.calls.append(a)
        ep.set_call_audio_focus(None)       # nothing focused: mic off every call
        rate = _far_end_rtp_rate(rig, b)
        _keypad(rig, ep, a)
        got = getattr(b, "digits", "")
        print(f"  unfocused call: far-end RTP {rate} pkt/s, DTMF received {got!r}")
        a.hangup(pj.CallOpParam(True)); rig.pump(0.4)
    return rate > 0 and got == "159#"


def scenario_muted_call(rig):
    """Mute button (set_call_mute) must not stop RTP or swallow DTMF."""
    with _bound_endpoint(rig) as ep:
        a, b = connected_pair(rig)
        rig.caller.calls.append(a)
        ep.set_call_audio_focus(a.getInfo().id)
        ep.set_call_mute(a, True)
        rate = _far_end_rtp_rate(rig, b)
        _keypad(rig, ep, a)
        got = getattr(b, "digits", "")
        print(f"  muted call: far-end RTP {rate} pkt/s, DTMF received {got!r}")
        ep._muted_call_ids.clear()
        a.hangup(pj.CallOpParam(True)); rig.pump(0.4)
    return rate > 0 and got == "159#"


SCENARIOS = {
    "unfocused_call": scenario_unfocused_call,
    "muted_call": scenario_muted_call,
    "dtmf_inband": scenario_dtmf_inband,
    "hold_resume_cycles": scenario_hold_resume_cycles,
    "hold_resume": scenario_hold_resume,
    "resume_via_reinvite": scenario_resume_via_reinvite,
    "cancel_while_ringing": scenario_cancel_while_ringing,
    "reject_603": scenario_reject_603,
    "incoming_default_hangup": scenario_incoming_default_hangup,
    "dtmf_rfc2833": scenario_dtmf_rfc2833,
    "dtmf_info": scenario_dtmf_info,
    "pj_error_detail": scenario_pj_error_detail,
}

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    from PySide6.QtCore import QCoreApplication
    _app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    names = sys.argv[1:] or list(SCENARIOS)
    print(f"pjsua2 source={PJSUA2_SOURCE}")
    rig = Rig()
    results = {}
    try:
        for n in names:
            print(f"== {n}")
            try:
                results[n] = SCENARIOS[n](rig)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                results[n] = f"CRASH {type(e).__name__}: {e}"
            print(f"   -> {'PASS' if results[n] is True else 'FAIL'} ({results[n]})")
    finally:
        rig.close()
    failed = [n for n, r in results.items() if r is not True]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} passed" + (f"; failed: {failed}" if failed else ""))
    sys.exit(len(failed))
