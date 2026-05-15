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
    if not spec.callers or not spec.targets:
        return []

    pairs: list[tuple[str, str]]
    if spec.mode == "matrix":
        pairs = [(caller, target) for caller in spec.callers for target in spec.targets]
    elif spec.mode == "paired":
        pairs = list(zip(spec.callers, spec.targets, strict=False))
    elif spec.mode == "fan-out":
        pairs = [(spec.callers[0], target) for target in spec.targets]
    elif spec.mode == "fan-in":
        pairs = [(caller, spec.targets[0]) for caller in spec.callers]
    else:
        raise ValueError(f"Unknown test plan mode: {spec.mode}")

    return [
        TestCall(index=index, caller_number=caller, target_number=target)
        for index, (caller, target) in enumerate(pairs, start=1)
    ]
