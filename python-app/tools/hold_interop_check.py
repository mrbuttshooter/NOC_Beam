r"""Hold / unhold interop check: the app's REAL SipEndpoint (real account
config) against an independent raw-UDP "switch" that follows RFC 3261
strictly -- in particular 14.2: a re-INVITE arriving while a previous INVITE
transaction is still open gets 500 Server Internal Error (491 if our own
re-INVITE is pending).

    cd python-app
    .venv\Scripts\python.exe <this file> [scenario ...]
"""
import os
import random
import re
import socket
import sys
import threading
import time

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(APP, "src"))
from PySide6.QtCore import QCoreApplication  # noqa: E402

qapp = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from noc_beam.config.store import AccountConfig, GlobalSettings  # noqa: E402
from noc_beam.sip._pjsua2_loader import pj  # noqa: E402
from noc_beam.sip.endpoint import SipEndpoint  # noqa: E402
from noc_beam.sip.events import sip_events  # noqa: E402

HOST = "127.0.0.1"
NB_PORT = 5098
SW_PORT = 5070
MEDIA = {0: "NONE", 1: "ACTIVE", 2: "LOCAL_HOLD", 3: "REMOTE_HOLD", 4: "ERROR"}
COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact",
           "l": "content-length", "c": "content-type", "k": "supported"}
T0 = time.monotonic()


def ts():
    return f"{time.monotonic() - T0:7.2f}"


class Msg:
    def __init__(self, data):
        text = data.decode("utf-8", "replace")
        head, _, self.body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        self.start = lines[0]
        self.hdrs = []
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                k = k.strip().lower()
                self.hdrs.append((COMPACT.get(k, k), v.strip()))
        self.is_req = not self.start.startswith("SIP/2.0")
        if self.is_req:
            self.method = self.start.split()[0]
        else:
            self.code = int(self.start.split()[1])
        cseq = self.get("cseq").split()
        self.cseq_n = int(cseq[0]) if cseq else 0
        self.cseq_m = cseq[1] if len(cseq) > 1 else ""

    def get(self, name):
        for k, v in self.hdrs:
            if k == name:
                return v
        return ""

    def all(self, name):
        return [v for k, v in self.hdrs if k == name]


def sdp_summary(body):
    if "m=audio" not in body:
        return "no-SDP"
    d = "sendrecv"
    for x in ("sendonly", "recvonly", "inactive"):
        if f"a={x}" in body:
            d = x
    m = re.search(r"m=audio (\d+) \S+ ([\d ]+)", body)
    o = re.search(r"o=\S+ (\d+) (\d+) IN IP4 (\S+)", body)
    c = re.search(r"c=IN IP4 (\S+)", body)
    return (f"{d} pts=[{m.group(2).strip() if m else '?'}] port={m.group(1) if m else '?'} "
            f"c={c.group(1) if c else '?'} o=v{o.group(2) if o else '?'}")


def tag_of(h):
    m = re.search(r";tag=([^;>\s]+)", h)
    return m.group(1) if m else ""


class Switch(threading.Thread):
    """Minimal strict UAS/UAC for one dialog at a time."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((HOST, SW_PORT))
        self.sock.settimeout(0.02)
        self.log = []
        self.events = []
        self.lock = threading.RLock()
        self.stop_flag = False
        self.timers = []
        self.reset()

    def reset(self, multi_codec=False, reinvite_delay=0.0, answer_delay=0.3, b2bua_guard=0.3):
        with self.lock:
            self.b2bua_guard = b2bua_guard
            self.d = None
            self.multi_codec = multi_codec
            self.reinvite_delay = reinvite_delay
            self.answer_delay = answer_delay
            self.events.clear()
            self.timers.clear()

    def note(self, kind, text):
        self.events.append((kind, text))
        self.log.append(f"{ts()} [{kind}] {text}")

    def send(self, text, addr):
        self.sock.sendto(text.encode(), addr)

    def run(self):
        while not self.stop_flag:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                data = None
            except OSError:
                data = None
            with self.lock:
                if data:
                    try:
                        self.handle(Msg(data), addr)
                    except Exception as e:  # noqa: BLE001
                        self.note("ERR", f"handler {e!r}")
                now = time.monotonic()
                due = [t for t in self.timers if t[0] <= now]
                self.timers = [t for t in self.timers if t[0] > now]
                for _, fn in due:
                    try:
                        fn()
                    except Exception as e:  # noqa: BLE001
                        self.note("ERR", f"timer {e!r}")

    def later(self, secs, fn):
        self.timers.append((time.monotonic() + secs, fn))

    def make_sdp(self, offer_body, direction=None, zero=False):
        d = self.d
        d["sdp_ver"] += 1
        pts = ["8", "101"]
        if offer_body and "m=audio" in offer_body:
            m = re.search(r"m=audio \d+ \S+ ([\d ]+)", offer_body)
            offered = m.group(1).split() if m else []
            te = [p for p in offered if re.search(rf"a=rtpmap:{p} telephone-event", offer_body)]
            first = "8" if "8" in offered else ("0" if "0" in offered else (offered[0] if offered else "8"))
            pts = [first] + (["0"] if self.multi_codec and first != "0" and "0" in offered else []) + te[:1]
        lines = ["v=0", f"o=switch 4242 {d['sdp_ver']} IN IP4 {HOST}", "s=SwitchSim",
                 f"c=IN IP4 {'0.0.0.0' if zero else HOST}", "t=0 0", f"m=audio 40000 RTP/AVP {' '.join(pts)}"]
        names = {"0": "PCMU/8000", "8": "PCMA/8000", "9": "G722/8000", "18": "G729/8000"}
        for p in pts:
            if p in names:
                lines.append(f"a=rtpmap:{p} {names[p]}")
            else:
                lines += [f"a=rtpmap:{p} telephone-event/8000", f"a=fmtp:{p} 0-16"]
        if direction:
            lines.append(f"a={direction}")
        return "\r\n".join(lines) + "\r\n"

    @staticmethod
    def answer_dir(offer_body):
        for a, b in (("sendonly", "recvonly"), ("recvonly", "sendonly"), ("inactive", "inactive")):
            if f"a={a}" in offer_body:
                return b
        if "c=IN IP4 0.0.0.0" in offer_body:
            return "recvonly"
        return "sendrecv"

    def respond(self, req, addr, code, reason, body="", extra=()):
        d = self.d
        to = req.get("to")
        if d and ";tag=" not in to:
            to += f";tag={d['ltag']}"
        lines = [f"SIP/2.0 {code} {reason}"] + [f"Via: {v}" for v in req.all("via")]
        lines += [f"From: {req.get('from')}", f"To: {to}", f"Call-ID: {req.get('call-id')}",
                  f"CSeq: {req.get('cseq')}", f"Contact: <sip:switch@{HOST}:{SW_PORT}>", "Server: SwitchSim"]
        lines += list(extra)
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body.encode())}")
        text = "\r\n".join(lines) + "\r\n\r\n" + body
        self.send(text, addr)
        self.log.append(f"{ts()} TX {code} {reason} (CSeq {req.get('cseq')}) {sdp_summary(body) if body else ''}")
        return text

    def handle(self, m, addr):
        if m.is_req:
            self.log.append(f"{ts()} RX {m.start.split(' sip')[0]} CSeq {m.cseq_n} {sdp_summary(m.body)}")
        else:
            self.log.append(f"{ts()} RX {m.code} (CSeq {m.cseq_n} {m.cseq_m}) {sdp_summary(m.body) if m.body else ''}")
        if not m.is_req:
            return self.on_response(m, addr)
        d = self.d
        if m.method == "INVITE" and not tag_of(m.get("to")):
            if d and d["call_id"] == m.get("call-id"):
                return
            self.d = d = {"call_id": m.get("call-id"), "ltag": f"sw{random.randint(1000, 9999)}",
                          "peer_from": m.get("from"), "our_uri": m.get("to"), "addr": addr,
                          "target": re.search(r"<([^>]+)>", m.get("contact")).group(1),
                          "rcseq": m.cseq_n, "lcseq": 100, "sdp_ver": 0, "srv_pending": m.cseq_n,
                          "cli_pending": None, "alive": True, "last_done": 0.0}
            self.respond(m, addr, 100, "Trying")
            self.respond(m, addr, 180, "Ringing")
            self.later(self.answer_delay, lambda: self._final_200(m, addr))
            return
        if not d or m.get("call-id") != d["call_id"]:
            self.respond(m, addr, 481, "Call/Transaction Does Not Exist")
            return
        if m.method == "INVITE":
            if m.cseq_n == d["rcseq"] and d["srv_pending"]:
                return
            if m.cseq_n <= d["rcseq"]:
                self.note("VIOLATION", f"re-INVITE CSeq {m.cseq_n} not above {d['rcseq']} -> 500")
                self.respond(m, addr, 500, "Server Internal Error")
                return
            gap = time.monotonic() - d["last_done"]
            if not d["srv_pending"] and not d["cli_pending"] and gap < self.b2bua_guard:
                self.note("VIOLATION", f"re-INVITE CSeq {m.cseq_n} only {gap*1000:.0f} ms after the previous INVITE "
                          f"finished (B2BUA still bridging the other leg) -> 500")
                self.respond(m, addr, 500, "Server Internal Error", extra=["Retry-After: 1"])
                return
            if d["srv_pending"]:
                self.note("VIOLATION", f"re-INVITE CSeq {m.cseq_n} while INVITE CSeq {d['rcseq']} still open (no ACK yet) -> 500")
                self.respond(m, addr, 500, "Server Internal Error", extra=["Retry-After: 2"])
                return
            if d["cli_pending"]:
                self.note("GLARE", f"re-INVITE CSeq {m.cseq_n} while our re-INVITE is pending -> 491")
                self.respond(m, addr, 491, "Request Pending")
                return
            d["rcseq"] = m.cseq_n
            self.note("REINVITE_IN", sdp_summary(m.body))
            self.respond(m, addr, 100, "Trying")
            d["srv_pending"] = m.cseq_n
            self.later(self.reinvite_delay, lambda: self._final_200(m, addr))
            return
        if m.method == "ACK":
            if d["srv_pending"] == m.cseq_n:
                d["srv_pending"] = None
                d["last_done"] = time.monotonic()
            if m.body:
                self.log.append(f"{ts()}    ACK carries SDP answer: {sdp_summary(m.body)}")
            return
        if m.method == "BYE":
            self.note("BYE_FROM_NOCBEAM", f"CSeq {m.cseq_n}")
            d["alive"] = False
            self.respond(m, addr, 200, "OK")
            return
        if m.method == "UPDATE":
            self.note("UPDATE_IN", sdp_summary(m.body))
            body = self.make_sdp(m.body, self.answer_dir(m.body)) if m.body else ""
            self.respond(m, addr, 200, "OK", body)
            return
        self.respond(m, addr, 200, "OK")

    def _final_200(self, req, addr):
        d = self.d
        if not d or not d["alive"]:
            return
        if req.body and "m=audio" in req.body:
            body = self.make_sdp(req.body, self.answer_dir(req.body))
        else:
            body = self.make_sdp(None, "sendrecv")
        text = self.respond(req, addr, 200, "OK", body)

        def retx(n=1):
            if d["srv_pending"] != req.cseq_n or not d["alive"]:
                return
            if n >= 16:
                self.note("NO_ACK", f"never got ACK for 200 to CSeq {req.cseq_n}; sending BYE")
                self.send_bye()
                return
            self.send(text, addr)
            self.later(0.5, lambda: retx(n + 1))
        self.later(0.5, retx)

    def _req(self, method, body="", cseq=None, branch=None):
        d = self.d
        if cseq is None:
            d["lcseq"] += 1
            cseq = d["lcseq"]
        branch = branch or f"z9hG4bKsw{random.randint(10**8, 10**9)}"
        frm = d["our_uri"] if ";tag=" in d["our_uri"] else f"{d['our_uri']};tag={d['ltag']}"
        lines = [f"{method} {d['target']} SIP/2.0", f"Via: SIP/2.0/UDP {HOST}:{SW_PORT};branch={branch};rport",
                 "Max-Forwards: 70", f"From: {frm}", f"To: {d['peer_from']}", f"Call-ID: {d['call_id']}",
                 f"CSeq: {cseq} {'ACK' if method == 'ACK' else method}", f"Contact: <sip:switch@{HOST}:{SW_PORT}>"]
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body.encode())}")
        # A real switch sends in-dialog requests to the Contact, not to the
        # address the INVITE happened to come from.
        u = d["target"].split("@")[-1].split(";")[0]
        host, _, port = u.partition(":")
        dest = (host, int(port or 5060))
        self.send("\r\n".join(lines) + "\r\n\r\n" + body, dest)
        self.log.append(f"{ts()} TX {method} CSeq {cseq} to {dest[0]}:{dest[1]} {sdp_summary(body) if method == 'INVITE' else ''}")
        return cseq, branch

    def send_reinvite(self, style):
        """style: sendonly | inactive | zero | sendrecv | nosdp"""
        with self.lock:
            d = self.d
            if style == "nosdp":
                body = ""
            elif style == "zero":
                body = self.make_sdp(None, None, zero=True)
            else:
                body = self.make_sdp(None, style)
            cseq, branch = self._req("INVITE", body)
            d["cli_pending"] = (cseq, branch, style)

            def lost():
                if d["cli_pending"] and d["cli_pending"][0] == cseq and d["alive"]:
                    self.note("NO_RESPONSE", f"our {style} re-INVITE to the Contact was never answered "
                              "-> switch releases the call")
                    d["cli_pending"] = None
                    d["alive"] = False
            self.later(4.0, lost)

    def send_bye(self):
        with self.lock:
            if self.d and self.d["alive"]:
                self._req("BYE")
                self.d["alive"] = False

    def on_response(self, m, addr):
        d = self.d
        if not d or m.cseq_m != "INVITE" or m.code < 200:
            return
        pend = d["cli_pending"]
        if m.code >= 300:
            self.note("REINVITE_REJECTED", f"NOC_Beam answered {m.code} to our {pend[2] if pend else '?'} re-INVITE")
            if pend:
                self._req("ACK", cseq=pend[0], branch=pend[1])
            d["cli_pending"] = None
            return
        body = ""
        if pend and pend[2] == "nosdp" and m.body:
            body = self.make_sdp(m.body, self.answer_dir(m.body))
        self._req("ACK", body, cseq=m.cseq_n)
        d["last_done"] = time.monotonic()
        if pend and pend[0] == m.cseq_n:
            self.note("REINVITE_OK", f"{pend[2]} -> NOC_Beam answered {sdp_summary(m.body)}")
            d["cli_pending"] = None


# --------------------------------------------------------------------- app side
def pump(secs):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


def until(pred, secs):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.005)
    return False


class App:
    def __init__(self):
        self.ep = SipEndpoint.instance()
        settings = GlobalSettings()
        settings.sip_port = 0   # the app default: random bind
        self.acc = AccountConfig(id="noc", label="noc", username="noc", domain=HOST, port=SW_PORT, register=False)
        self.ep.start(settings, [self.acc])
        assert self.ep.is_started()
        self.ep._ep.audDevManager().setNullDev()
        self.ep.add_account(self.acc)
        self.ended = []
        ev = sip_events()
        ev.call_ended.connect(self.ended.append)
        # What PhoneShell._on_call_media does: re-route focus to the call.
        ev.call_media_active.connect(lambda cid, *a: self.ep.set_call_audio_focus(cid))

    def media(self, call):
        try:
            self.ep._ensure_thread_registered()
            info = call.getInfo()
            return f"state={info.stateText} media=" + ",".join(MEDIA.get(m.status, "?") for m in info.media if m.type == 1)
        except Exception as e:  # noqa: BLE001
            return f"<gone: {type(e).__name__}>"

    def dial(self, sw):
        call = self.ep.make_call("noc", f"sip:447700900123@{HOST}:{SW_PORT}")
        ok = until(lambda: sw.d is not None and sw.d["srv_pending"] is None and sw.d.get("alive"), 5)
        pump(0.4)
        assert ok, "call did not connect"
        return call


def scenario(name, fn, app, sw, **sw_opts):
    sw.reset(**sw_opts)
    mark = len(sw.log)
    print(f"\n=== {name} ===")
    call = app.dial(sw)
    cid = call.getId()
    app.ended.clear()
    try:
        fn(app, sw, call)
    except Exception as e:  # noqa: BLE001
        print(f"  !! driver raised {type(e).__name__}: {e}")
    pump(3.0)
    alive = cid not in app.ended and sw.d["alive"]
    print(f"  NOC_Beam leg: {app.media(call)}")
    for line in sw.log[mark:]:
        print("   ", line)
    bad = [e for e in sw.events if e[0] in ("VIOLATION", "NO_RESPONSE", "BYE_FROM_NOCBEAM", "NO_ACK", "REINVITE_REJECTED", "ERR", "GLARE")]
    verdict = "PASS" if alive and not bad else "FAIL"
    print(f"  -> {verdict}  (call alive={alive}; problems={bad})")
    if sw.d["alive"]:
        try:
            app.ep.hangup_call(call)
        except Exception:
            pass
        pump(0.8)
    return verdict == "PASS"


def far_hold(style_hold, style_unhold):
    def fn(app, sw, call):
        sw.send_reinvite(style_hold)
        pump(1.5)
        print(f"  after far hold   : {app.media(call)}")
        sw.send_reinvite(style_unhold)
        pump(1.5)
        print(f"  after far unhold : {app.media(call)}")
    return fn


def nb_hold_resume(resume, gap=1.5):
    def fn(app, sw, call):
        app.ep.hold_call(call)
        pump(gap)
        print(f"  after NB hold    : {app.media(call)}")
        if resume == "app":
            app.ep.resume_call(call)
        elif resume == "v130":  # what v1.3.0 (main) does: setHold again
            call.setHold(pj.CallOpParam(True))
        pump(1.5)
        print(f"  after NB resume  : {app.media(call)}")
    return fn


SCENARIOS = {
    "far_sendonly": (far_hold("sendonly", "sendrecv"), {}),
    "far_inactive": (far_hold("inactive", "sendrecv"), {}),
    "far_zero_ip": (far_hold("zero", "sendrecv"), {}),
    "far_unhold_nosdp": (far_hold("sendonly", "nosdp"), {}),
    "nb_hold_resume": (nb_hold_resume("app"), {}),
    "nb_hold_resume_v130": (nb_hold_resume("v130"), {}),
    "nb_fast_resume_slow_switch": (nb_hold_resume("app", gap=0.3), {"reinvite_delay": 1.5}),
    "nb_hold_multicodec": (nb_hold_resume("app"), {"multi_codec": True}),
    "far_hold_multicodec": (far_hold("sendonly", "sendrecv"), {"multi_codec": True}),
    "far_unhold_nosdp_multicodec": (far_hold("sendonly", "nosdp"), {"multi_codec": True}),
}


def main():
    args = sys.argv[1:]
    if "--old-contact" in args:  # undo the Contact-port fix
        args.remove("--old-contact")
        from noc_beam.sip import account as acc_mod
        orig_init = acc_mod.SipAccount.__init__

        def init(self, cfg, transports, local_sip_port=5060, transport_ports=None):
            orig_init(self, cfg, transports, local_sip_port, transport_ports=None)
        acc_mod.SipAccount.__init__ = init
        print("** running with the OLD Contact port (sip_port or 5060) **")
    if "--pjsip-default" in args:  # undo the fix: PJSIP's lockCodecEnabled=True
        args.remove("--pjsip-default")
        from noc_beam.sip import account as acc_mod
        orig = acc_mod.SipAccount.configure

        def configure(self):
            ac = orig(self)
            ac.mediaConfig.lockCodecEnabled = True
            return ac
        acc_mod.SipAccount.configure = configure
        print("** running with PJSIP default lockCodecEnabled=True (pre-fix) **")
    names = args or list(SCENARIOS)
    sw = Switch()
    sw.start()
    app = App()
    results = {}
    try:
        for n in names:
            fn, opts = SCENARIOS[n]
            results[n] = scenario(n, fn, app, sw, **opts)
    finally:
        pump(0.5)
        app.ep.stop()
        sw.stop_flag = True
    print("\nSUMMARY")
    for n, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")


if __name__ == "__main__":
    main()
