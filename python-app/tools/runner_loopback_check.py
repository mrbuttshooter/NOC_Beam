r"""Loopback check for the Test Runner on the REAL native engine.

Drives the real TestRunner through the real SipEndpoint. A scripted fake
carrier on the same engine answers each INVITE by the dialled user part:
  ring      180, never answers            busy   486 Busy Here
  answer    180 then 200 after 300 ms     cong   503 Service Unavailable
  e183      183 only, never 180/200       byeN   answer, then BYE after N s
Every verdict is read from the runner's own call_completed / run_complete.

Covers: verdict per outcome (reachability + full-call), parallel x times,
Stop mid-run, call-id wrap-around, slot exhaustion backpressure, custom
account port, ORIGINATION A-number, FAS-sweep jitter, back-to-back runs.

    cd python-app
    .venv/Scripts/python.exe tools/runner_loopback_check.py          # all
    .venv/Scripts/python.exe tools/runner_loopback_check.py cancel

Loopback note: the fake carrier's legs use the same engine's call slots,
so each call costs two. Exit code = number of failing scenarios.
Port: HARNESS_PORT (5098).
"""
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402

app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

from noc_beam.config.store import AccountConfig, GlobalSettings  # noqa: E402
from noc_beam.sip._pjsua2_loader import pj  # noqa: E402
from noc_beam.sip.endpoint import SipEndpoint  # noqa: E402
from noc_beam.sip.events import sip_events  # noqa: E402
from noc_beam.testing.plan import TestSpec  # noqa: E402
from noc_beam.testing.runner import TestRunner  # noqa: E402

PORT = int(os.environ.get("HARNESS_PORT", "5098"))
HOST = "127.0.0.1"


def pump(secs):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def until(pred, secs):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.005)
    return False


class FarEnd:
    """Scripted carrier: reacts to call_incoming on the shared engine."""

    def __init__(self, ep):
        self.ep = ep
        self.seen = Counter()
        self.remotes = []
        sip_events().call_incoming.connect(self._on_incoming)

    @staticmethod
    def _user(uri):
        u = uri.split("sip:", 1)[-1]
        return u.split("@", 1)[0]

    def _answer(self, call, code):
        try:
            prm = pj.CallOpParam(True)
            prm.statusCode = code
            call.answer(prm)
        except Exception:
            pass

    def _hangup(self, call, code=None):
        try:
            prm = pj.CallOpParam(True)
            if code:
                prm.statusCode = code
            call.hangup(prm)
        except Exception:
            pass

    def _on_incoming(self, account_id, call_id, remote, is_in):
        call = self.ep.find_call(call_id)
        if call is None:
            return
        try:
            behaviour = self._user(call.getInfo().localUri)
        except Exception:
            return
        self.seen[behaviour] += 1
        self.remotes.append(remote)
        if behaviour == "ring":
            self._answer(call, 180)
        elif behaviour == "answer":
            self._answer(call, 180)
            QTimer.singleShot(300, lambda c=call: self._answer(c, 200))
        elif behaviour == "busy":
            self._hangup(call, 486)
        elif behaviour == "cong":
            self._hangup(call, 503)
        elif behaviour == "e183":
            self._answer(call, 183)
        elif behaviour.startswith("bye"):
            secs = float(behaviour[3:] or 1)
            self._answer(call, 200)
            QTimer.singleShot(int(secs * 1000), lambda c=call: self._hangup(c))
        else:
            self._hangup(call, 404)


class Rig:
    def __init__(self):
        self.ep = SipEndpoint.instance()
        settings = GlobalSettings()
        settings.sip_port = PORT
        self.caller = AccountConfig(id="caller-acc", label="caller", username="caller",
                                    domain=HOST, register=False)
        self.far = AccountConfig(id="far-acc", label="far", username="far",
                                 domain=HOST, register=False)
        self.ep.start(settings, [self.caller, self.far])
        assert self.ep.is_started(), "endpoint failed to start"
        self.ep._ep.audDevManager().setNullDev()
        self.ep.add_account(self.caller)
        self.ep.add_account(self.far)
        self.farend = FarEnd(self.ep)

    def live_calls(self):
        n = 0
        for acc in self.ep.accounts():
            for c in list(acc.calls):
                try:
                    if int(c.getInfo().state) != 6:
                        n += 1
                except Exception:
                    pass
        return n

    def run(self, spec, accounts=None, deadline=60.0, cancel_after=None):
        results, done = [], []
        runner = TestRunner(spec, accounts or [self.caller], endpoint=self.ep,
                            events=sip_events(), active_account_id=self.caller.id)
        runner.call_completed.connect(results.append)
        runner.run_complete.connect(done.append)
        t0 = time.monotonic()
        runner.start()
        if cancel_after is not None:
            until(lambda: bool(done), cancel_after)
            if not done:
                runner.cancel()
        until(lambda: bool(done), deadline)
        elapsed = time.monotonic() - t0
        return runner, results, done, elapsed

    def close(self):
        pump(0.5)
        self.ep.stop()


def target(behaviour):
    return f"sip:{behaviour}@{HOST}:{PORT}"


def spec(targets, mode="matrix", crit="reachability", parallel=1, hold=1.0,
         timeout=4.0, times=1, cps=0.0):
    return TestSpec(callers=[], targets=targets, mode=mode, pass_criterion=crit,
                    parallel=parallel, hold_seconds=hold, timeout_seconds=timeout,
                    times=times, max_cps=cps)


def show(results, limit=12):
    for r in sorted(results, key=lambda r: r.call.index)[:limit]:
        print(f"    #{r.call.index:<3} {r.call.target_number.split('@')[0]:<18} {r.result:<5} "
              f"{r.sip_code!s:<4} {r.sip_reason[:24]:<24} {r.notes[:34]:<34} {r.duration_s:5.2f}s")


# ------------------------------------------------------------------ scenarios
def s_reachability_outcomes(rig):
    """Each carrier outcome maps to the right verdict and code."""
    t = [target(b) for b in ("ring", "answer", "busy", "cong", "e183")]
    _, res, done, el = rig.run(spec(t, timeout=3.0), deadline=40)
    show(res)
    by = {r.call.target_number.split(":")[1].split("@")[0]: r for r in res}
    ok = (len(res) == 5 and len(done) == 1
          and by["ring"].result == "PASS" and by["ring"].sip_code == 180
          and by["answer"].result == "PASS"
          and by["busy"].result == "FAIL" and by["busy"].sip_code == 486
          and by["cong"].result == "FAIL" and by["cong"].sip_code == 503
          and by["e183"].result == "FAIL" and by["e183"].notes == "early_media_only_no_180")
    pump(1.5)
    print(f"  results={len(res)} run_complete={len(done)} live_calls_after={rig.live_calls()}")
    return ok and rig.live_calls() == 0


def s_full_call(rig):
    """full-call: answered call held for hold_seconds then PASS; far-end
    hang-up before the hold ends; no answer times out."""
    t = [target("answer"), target("bye1"), target("ring")]
    _, res, done, el = rig.run(spec(t, crit="full-call", hold=2.0, timeout=3.0), deadline=40)
    show(res)
    by = {r.call.target_number.split(":")[1].split("@")[0]: r for r in res}
    pump(1.5)
    print(f"  results={len(res)} run_complete={len(done)} live_calls_after={rig.live_calls()}")
    return (len(res) == 3 and by["answer"].result == "PASS" and by["answer"].duration_s >= 2.0
            and by["ring"].result == "FAIL" and by["ring"].sip_code == 408
            and rig.live_calls() == 0)


def s_parallel_times(rig):
    """times x targets with parallel: every call exactly once, one run_complete."""
    t = [target("busy"), target("answer")]
    _, res, done, el = rig.run(spec(t, parallel=4, times=3, timeout=3.0), deadline=60)
    idx = Counter(r.call.index for r in res)
    dup = [i for i, n in idx.items() if n > 1]
    pump(1.5)
    print(f"  results={len(res)} unique={len(idx)} dups={dup} run_complete={len(done)} "
          f"elapsed={el:.1f}s live_calls_after={rig.live_calls()}")
    print(f"  verdicts: {Counter((r.call.target_number.split(':')[1].split('@')[0], r.result, r.sip_code) for r in res)}")
    return len(res) == 6 and not dup and len(done) == 1 and rig.live_calls() == 0


def s_cancel(rig):
    """Stop mid-run: every call reported, calls torn down, one run_complete."""
    sp = spec([target("ring"), target("e183"), target("answer")], parallel=3, times=4,
              timeout=20.0, crit="full-call", hold=20.0)
    _, res, done, el = rig.run(sp, deadline=15, cancel_after=2.0)
    pump(2.0)
    live = rig.live_calls()
    print(f"  results={len(res)} (expect 12) run_complete={len(done)} live_calls_after={live}")
    print(f"  {Counter((r.result, r.sip_reason) for r in res)}")
    return len(res) == 12 and len(done) == 1 and live == 0


def s_id_wrap_stress(rig, parallel=12):
    """Many fast calls so PJSIP call-ids wrap around the slot table: no
    result may be charged to the wrong call. Loopback note: the fake
    carrier's legs use the SAME engine's call slots, so each call costs 2;
    parallel 12 keeps both legs inside the 32-slot table."""
    t = [target("ring"), target("busy"), target("cong"), target("answer")]
    _, res, done, el = rig.run(spec(t, parallel=parallel, times=12, timeout=4.0), deadline=120)
    pump(2.0)
    kinds = Counter((r.call.target_number.split(":")[1].split("@")[0], r.result, r.sip_code, r.notes[:20])
                    for r in res)
    print(f"  results={len(res)} (expect 48) run_complete={len(done)} elapsed={el:.1f}s "
          f"live_calls_after={rig.live_calls()}")
    for k, n in sorted(kinds.items()):
        print(f"    {n:3d} x {k}")
    expect = {"ring": ("PASS", 180), "busy": ("FAIL", 486), "cong": ("FAIL", 503), "answer": ("PASS", 200)}
    wrong = [r for r in res
             if (r.result, r.sip_code) != expect[r.call.target_number.split(":")[1].split("@")[0]]
             and not (r.call.target_number.startswith("sip:answer") and r.result == "PASS")]
    return len(res) == 48 and len(done) == 1 and not wrong and rig.live_calls() == 0


def s_account_port(rig):
    """Bare number on an account with a custom port must reach that port."""
    ported = AccountConfig(id="ported-acc", label="ported", username="ported",
                           domain=HOST, port=PORT, register=False)
    rig.ep.add_account(ported)
    _, res, done, el = rig.run(spec(["answer"], timeout=4.0), accounts=[ported], deadline=20)
    show(res)
    rig.ep.remove_account(ported.id)
    pump(1.0)
    return len(res) == 1 and res[0].result == "PASS"


def s_instant_fail(rig):
    """Unroutable targets: DNS failure reports its real cause at once; a
    TCP port that never answers is a genuine no-response and times out."""
    t = ["sip:x@127.0.0.1:9;transport=tcp", "sip:y@nonexistent-host.invalid"]
    _, res, done, el = rig.run(spec(t, parallel=2, timeout=5.0), deadline=40)
    show(res)
    pump(1.0)
    by = {r.call.target_number.split("@")[1][:9]: r for r in res}
    dns = by["nonexisten"[:9]]
    tcp = by["127.0.0.1"]
    return (len(res) == 2 and dns.sip_code == 502 and dns.duration_s < 2.0
            and tcp.sip_code == 408 and tcp.notes == "timeout")


def s_two_runs(rig):
    """Back-to-back runs on the shared event bus stay isolated."""
    _, r1, d1, _ = rig.run(spec([target("busy"), target("ring")], parallel=2), deadline=20)
    _, r2, d2, _ = rig.run(spec([target("cong")], parallel=1, times=3), deadline=20)
    print(f"  run1 results={len(r1)} complete={len(d1)}  run2 results={len(r2)} complete={len(d2)}")
    print(f"  run2: {Counter((r.result, r.sip_code) for r in r2)}")
    return len(r1) == 2 and len(r2) == 3 and len(d1) == 1 and len(d2) == 1         and all(r.sip_code == 503 for r in r2)


def s_slot_backpressure(rig):
    """Slot table exhausted (parallel 16 on loopback = 32 legs + teardown):
    the runner must never record PJ_ETOOMANY as a call result."""
    t = [target("busy"), target("cong")]
    _, res, done, el = rig.run(spec(t, parallel=16, times=24, timeout=4.0), deadline=180)
    pump(2.0)
    bad = [r for r in res if "Too many" in (r.notes or "") or "Too many" in (r.sip_reason or "")
           or r.sip_code == 0]
    print(f"  results={len(res)} (expect 48) slot-error results={len(bad)} run_complete={len(done)} "
          f"elapsed={el:.1f}s live_calls_after={rig.live_calls()}")
    print(f"  {Counter((r.result, r.sip_code) for r in res)}")
    return len(res) == 48 and not bad and len(done) == 1 and rig.live_calls() == 0


def s_origination_a_number(rig):
    """An ORIGINATION number in Callers is dialled from the active account
    and presented to the far end as the caller ID."""
    rig.farend.remotes.clear()
    sp = TestSpec(callers=["+20 100-123-4567"], targets=[target("busy")], mode="fan-out",
                  pass_criterion="reachability", parallel=1, hold_seconds=1, timeout_seconds=4)
    _, res, done, el = rig.run(sp, deadline=20)
    show(res)
    print(f"  far end saw From: {rig.farend.remotes}")
    return len(res) == 1 and res[0].sip_code == 486 and any('"+201001234567"' in r for r in rig.farend.remotes)


def s_sweep_jitter(rig):
    """FAS sweep: repeat probes of one target are spaced by the jitter window."""
    sp = TestSpec(callers=[], targets=[target("busy")], mode="fas-sweep",
                  pass_criterion="reachability", parallel=4, hold_seconds=1,
                  timeout_seconds=4, tries_per_pair=3, jitter_low_s=1.0, jitter_high_s=1.3)
    _, res, done, el = rig.run(sp, deadline=30)
    starts = sorted(r.started_at for r in res)
    gaps = [round(b - a, 2) for a, b in zip(starts, starts[1:])]
    print(f"  results={len(res)} gaps between probes={gaps}s elapsed={el:.1f}s")
    return len(res) == 3 and all(g >= 1.0 for g in gaps)


SCENARIOS = {
    "origination_a_number": s_origination_a_number,
    "sweep_jitter": s_sweep_jitter,
    "instant_fail": s_instant_fail,
    "two_runs": s_two_runs,
    "reachability_outcomes": s_reachability_outcomes,
    "full_call": s_full_call,
    "parallel_times": s_parallel_times,
    "cancel": s_cancel,
    "id_wrap_stress": s_id_wrap_stress,
    "slot_backpressure": lambda rig: s_slot_backpressure(rig),
    "account_port": s_account_port,
}

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    names = sys.argv[1:] or list(SCENARIOS)
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
            print(f"   -> {'PASS' if results[n] is True else 'FAIL'}")
    finally:
        rig.close()
    failed = [n for n, r in results.items() if r is not True]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} passed" + (f"; failed: {failed}" if failed else ""))
    sys.exit(len(failed))
