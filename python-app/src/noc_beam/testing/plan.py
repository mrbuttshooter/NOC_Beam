from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TestMode = Literal["matrix", "paired", "fan-out", "fan-in", "fas-sweep"]
PassCriterion = Literal["reachability", "full-call"]

_VALID_MODES = ("matrix", "paired", "fan-out", "fan-in", "fas-sweep")

# Default calls-per-second cap applied by the Test Runner UI. The runner
# itself defaults max_cps=0.0 (unlimited) for backward compatibility, but
# the view passes this so production runs are paced. A fast-failing route
# (e.g. 503 "no circuit/channel available" returning in <50ms) otherwise
# lets the runner re-dial instantly on every freed slot, hammering the
# trunk and self-inflicting congestion.
DEFAULT_MAX_CPS = 5.0


def _clamp_int(value: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except Exception:
        v = lo
    return max(lo, min(hi, v))


@dataclass
class TestSpec:
    callers: list[str]
    targets: list[str]
    mode: TestMode
    pass_criterion: PassCriterion
    parallel: int
    hold_seconds: float
    timeout_seconds: float
    # Number of repeated dials per (caller, target) pair for the
    # non-fas-sweep modes. Clamped to [1, 50].
    times: int = 1
    # Number of repeated dials per (caller, target) pair for the
    # fas-sweep mode. Clamped to [1, 50]. Quick=2, Thorough=4 are
    # the operator-facing presets.
    tries_per_pair: int = 2
    # Inter-call jitter window for fas-sweep mode (seconds). Used by
    # the runtime scheduler to space sweep dials so far-end pattern
    # detection has cooling-off time between probes.
    jitter_low_s: float = 30.0
    jitter_high_s: float = 120.0
    # Calls-per-second ceiling for dispatch. 0.0 = unlimited (legacy
    # behaviour). >0 enforces a minimum gap of 1/max_cps between
    # consecutive dials, on top of the `parallel` concurrency cap.
    max_cps: float = 0.0

    def __post_init__(self) -> None:
        self.parallel = _clamp_int(self.parallel, 1, 16)
        self.hold_seconds = max(self.hold_seconds, 0.0)
        self.timeout_seconds = max(self.timeout_seconds, 0.1)
        self.max_cps = max(0.0, float(self.max_cps))
        self.times = _clamp_int(self.times, 1, 50)
        self.tries_per_pair = _clamp_int(self.tries_per_pair, 1, 50)
        # Jitter window: keep low <= high; both non-negative.
        try:
            self.jitter_low_s = max(0.0, float(self.jitter_low_s))
        except Exception:
            self.jitter_low_s = 30.0
        try:
            self.jitter_high_s = max(0.0, float(self.jitter_high_s))
        except Exception:
            self.jitter_high_s = 120.0
        if self.jitter_high_s < self.jitter_low_s:
            self.jitter_high_s = self.jitter_low_s


@dataclass(frozen=True)
class TestCall:
    index: int
    caller_number: str
    target_number: str


def normalise_lines(text: str) -> list[str]:
    return [line for raw_line in text.splitlines() if (line := raw_line.strip())]


def expand(spec: TestSpec) -> list[TestCall]:
    if spec.mode not in _VALID_MODES:
        raise ValueError(f"Unknown test plan mode: {spec.mode}")

    if not spec.targets:
        return []
    # Empty callers list -> treat as a single wildcard caller so the
    # runner's _resolve_account falls through to "use the active
    # account". The common demo workflow is "paste 20 numbers into
    # targets, leave callers blank, click Run" -- previously this
    # silently returned [] (no calls).
    callers = spec.callers if spec.callers else ["*"]

    pairs: list[tuple[str, str]]
    if spec.mode in ("matrix", "fas-sweep"):
        pairs = [(caller, target) for caller in callers for target in spec.targets]
    elif spec.mode == "paired":
        # Paired mode is a strict 1:1 zip — but historically (and per
        # operator mental model) "I have ONE caller and many targets,
        # call all of them from that caller" is the most common batch
        # shape. zip() of a 1-element list with N targets silently
        # drops N-1 targets, which is a footgun: operator pastes 100
        # numbers, clicks Run, sees only 1 call fire, the other 99
        # vanish without a trace.
        #
        # Defensive: when callers has exactly 1 entry but targets has
        # more, promote to fan-out semantics (that single caller is
        # used for every target). Same for the inverse (1 target,
        # many callers => fan-in). Otherwise zip as before.
        if len(callers) == 1 and len(spec.targets) > 1:
            pairs = [(callers[0], target) for target in spec.targets]
        elif len(spec.targets) == 1 and len(callers) > 1:
            pairs = [(caller, spec.targets[0]) for caller in callers]
        else:
            pairs = list(zip(callers, spec.targets, strict=False))
    elif spec.mode == "fan-out":
        pairs = [(callers[0], target) for target in spec.targets]
    else:
        # fan-in
        pairs = [(caller, spec.targets[0]) for caller in callers]

    # Multiplier: fas-sweep uses tries_per_pair; everything else uses times.
    multiplier = spec.tries_per_pair if spec.mode == "fas-sweep" else spec.times
    multiplier = max(1, int(multiplier))

    # Round-robin order: [t1, t2, t3, t1, t2, t3, ...] instead of the
    # older grouped order [t1, t1, ..., t2, t2, ...]. With the runner's
    # per-target serialization (only one in-flight call per distinct
    # target URI), the natural FIFO dispatch path now spreads concurrent
    # calls across DIFFERENT targets first — so 3 numbers × N times
    # actually runs 3-wide. With the old grouped order, every dispatch
    # past the first would collide on t1 and stall until t1 finished.
    expanded_pairs: list[tuple[str, str]] = []
    for _ in range(multiplier):
        for pair in pairs:
            expanded_pairs.append(pair)

    return [
        TestCall(index=index, caller_number=caller, target_number=target)
        for index, (caller, target) in enumerate(expanded_pairs, start=1)
    ]
