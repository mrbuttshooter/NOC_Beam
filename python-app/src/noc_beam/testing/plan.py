from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TestMode = Literal["matrix", "paired", "fan-out", "fan-in"]
PassCriterion = Literal["reachability", "full-call"]


@dataclass
class TestSpec:
    callers: list[str]
    targets: list[str]
    mode: TestMode
    pass_criterion: PassCriterion
    parallel: int
    hold_seconds: float
    timeout_seconds: float

    def __post_init__(self) -> None:
        self.parallel = min(max(self.parallel, 1), 16)
        self.hold_seconds = max(self.hold_seconds, 0.0)
        self.timeout_seconds = max(self.timeout_seconds, 0.1)


@dataclass(frozen=True)
class TestCall:
    index: int
    caller_number: str
    target_number: str


def normalise_lines(text: str) -> list[str]:
    return [line for raw_line in text.splitlines() if (line := raw_line.strip())]


def expand(spec: TestSpec) -> list[TestCall]:
    if spec.mode not in ("matrix", "paired", "fan-out", "fan-in"):
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
    if spec.mode == "matrix":
        pairs = [(caller, target) for caller in callers for target in spec.targets]
    elif spec.mode == "paired":
        pairs = list(zip(callers, spec.targets, strict=False))
    elif spec.mode == "fan-out":
        pairs = [(callers[0], target) for target in spec.targets]
    else:
        pairs = [(caller, spec.targets[0]) for caller in callers]

    return [
        TestCall(index=index, caller_number=caller, target_number=target)
        for index, (caller, target) in enumerate(pairs, start=1)
    ]
