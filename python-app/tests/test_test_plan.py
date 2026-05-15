from __future__ import annotations

import pytest

from noc_beam.testing import plan


def make_spec(
    callers: list[str],
    targets: list[str],
    mode: str,
    parallel: int = 4,
) -> plan.TestSpec:
    return plan.TestSpec(
        callers=callers,
        targets=targets,
        mode=mode,  # type: ignore[arg-type]
        pass_criterion="reachability",
        parallel=parallel,
        hold_seconds=5.0,
        timeout_seconds=30.0,
    )


def test_matrix_expands_in_caller_major_order() -> None:
    spec = make_spec(
        callers=["1001", "1002", "1003"],
        targets=["2001", "2002", "2003", "2004"],
        mode="matrix",
    )

    assert plan.expand(spec) == [
        plan.TestCall(1, "1001", "2001"),
        plan.TestCall(2, "1001", "2002"),
        plan.TestCall(3, "1001", "2003"),
        plan.TestCall(4, "1001", "2004"),
        plan.TestCall(5, "1002", "2001"),
        plan.TestCall(6, "1002", "2002"),
        plan.TestCall(7, "1002", "2003"),
        plan.TestCall(8, "1002", "2004"),
        plan.TestCall(9, "1003", "2001"),
        plan.TestCall(10, "1003", "2002"),
        plan.TestCall(11, "1003", "2003"),
        plan.TestCall(12, "1003", "2004"),
    ]


def test_paired_mismatched_lengths_uses_shorter_side() -> None:
    spec = make_spec(
        callers=["1001", "1002", "1003"],
        targets=["2001", "2002"],
        mode="paired",
    )

    assert plan.expand(spec) == [
        plan.TestCall(1, "1001", "2001"),
        plan.TestCall(2, "1002", "2002"),
    ]


def test_fan_out_uses_first_caller_for_all_targets() -> None:
    spec = make_spec(
        callers=["1001", "1002", "1003"],
        targets=["2001", "2002", "2003", "2004", "2005"],
        mode="fan-out",
    )

    assert plan.expand(spec) == [
        plan.TestCall(1, "1001", "2001"),
        plan.TestCall(2, "1001", "2002"),
        plan.TestCall(3, "1001", "2003"),
        plan.TestCall(4, "1001", "2004"),
        plan.TestCall(5, "1001", "2005"),
    ]


def test_fan_in_uses_first_target_for_all_callers() -> None:
    spec = make_spec(
        callers=["1001", "1002", "1003"],
        targets=["2001", "2002"],
        mode="fan-in",
    )

    assert plan.expand(spec) == [
        plan.TestCall(1, "1001", "2001"),
        plan.TestCall(2, "1002", "2001"),
        plan.TestCall(3, "1003", "2001"),
    ]


def test_normalise_lines_strips_drops_blanks_and_preserves_duplicates() -> None:
    assert plan.normalise_lines(" 1001 \n\n1002\n  \n1001\t\n") == [
        "1001",
        "1002",
        "1001",
    ]


@pytest.mark.parametrize("mode", ["matrix", "paired", "fan-out", "fan-in"])
def test_all_modes_return_empty_when_either_side_empty(mode: str) -> None:
    assert plan.expand(make_spec([], ["2001"], mode)) == []
    assert plan.expand(make_spec(["1001"], [], mode)) == []


@pytest.mark.parametrize(
    ("requested_parallel", "expected_parallel"),
    [
        (-3, 1),
        (0, 1),
        (1, 1),
        (8, 8),
        (16, 16),
        (17, 16),
    ],
)
def test_parallel_is_clamped_to_one_through_sixteen(
    requested_parallel: int,
    expected_parallel: int,
) -> None:
    spec = make_spec(["1001"], ["2001"], "paired", parallel=requested_parallel)

    assert spec.parallel == expected_parallel
