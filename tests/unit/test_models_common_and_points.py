"""The shared types and the points of a definition: each rejection says what to fix."""

import re

import pytest
from pydantic import ValidationError

from igs_iec104_mcp.models.common import (
    check_address,
    check_common_address,
    check_timing,
    duration_seconds,
    sha256_of,
)
from igs_iec104_mcp.models.points import Point


@pytest.mark.parametrize("value", [1, 65534])
def test_a_common_address_in_range_is_accepted(value: int) -> None:
    assert check_common_address(value) == value


@pytest.mark.parametrize("value", [0, 65535, 70000])
def test_a_common_address_out_of_range_is_refused(value: int) -> None:
    with pytest.raises(
        ValueError, match=r"common address .* out of range: use 1 to 65534"
    ):
        check_common_address(value)


@pytest.mark.parametrize("value", [1, 16_777_215])
def test_an_address_in_range_is_accepted(value: int) -> None:
    assert check_address(value) == value


@pytest.mark.parametrize("value", [0, 16_777_216])
def test_an_address_out_of_range_is_refused(value: int) -> None:
    with pytest.raises(ValueError, match="out of range: use 1 to 16777215"):
        check_address(value)


def test_a_duration_is_read_in_seconds_and_must_be_above_zero() -> None:
    assert duration_seconds("500ms") == 0.5
    assert duration_seconds("5m") == 300
    with pytest.raises(ValueError, match="must be a duration above zero"):
        check_timing("0s")


def test_a_duration_without_a_unit_is_refused_with_the_form_expected() -> None:
    with pytest.raises(ValueError, match="write a number and a unit"):
        check_timing("10")


def test_a_duration_longer_than_an_hour_is_refused() -> None:
    with pytest.raises(ValueError, match="longer than an hour"):
        check_timing("2h")


def test_the_hash_of_a_value_does_not_depend_on_the_order_of_its_keys() -> None:
    assert sha256_of({"a": 1, "b": [2]}) == sha256_of({"b": [2], "a": 1})


def _point(**fields: object) -> dict[str, object]:
    return {"address": 100, "kind": "single", "value": True, **fields}


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("single", True),
        ("double", "intermediate"),
        ("step", {"value": -64, "transient": False}),
        ("bitstring", 4_294_967_295),
        ("normalized", -1.0),
        ("scaled", -32_768),
        ("float", 3.5),
        ("counter", -2_147_483_648),
        ("packed", {"status": 65_535, "change": 0}),
        ("normalized_unqualified", 0.5),
    ],
)
def test_each_kind_takes_its_form(kind: str, value: object) -> None:
    assert Point.model_validate(_point(kind=kind, value=value)).kind == kind


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("single", 1, "a single point takes true or false"),
        ("double", "maybe", "takes one of"),
        ("step", {"value": 64, "transient": False}, "takes an object"),
        ("bitstring", -1, "takes an integer from 0"),
        ("normalized", 1.0, "a number from -1 up to 1"),
        ("normalized", 0.00001, re.escape("on a grid of 2^-15")),
        ("scaled", 40_000, "takes an integer from -32768 to 32767"),
        ("float", float("nan"), "takes a finite number"),
        ("counter", 2**31, "takes an integer from"),
        ("packed", {"status": 1}, "takes an object with 'status' and 'change'"),
    ],
)
def test_a_value_that_does_not_fit_its_kind_is_refused_with_the_form(
    kind: str, value: object, expected: str
) -> None:
    with pytest.raises(ValidationError, match=expected):
        Point.model_validate(_point(kind=kind, value=value))


def test_a_point_without_a_time_tag_cannot_be_stamped() -> None:
    with pytest.raises(ValidationError, match="has no time-tagged form"):
        Point.model_validate(
            _point(kind="packed", value={"status": 0, "change": 0}, stamped=True)
        )


def test_a_command_is_accepted_only_on_a_single_or_double_point() -> None:
    with pytest.raises(
        ValidationError, match="accepted only on a single or double point"
    ):
        Point.model_validate(_point(kind="float", value=1.0, command="accept"))


def test_a_toggle_needs_a_single_or_double_point() -> None:
    simulation = {"kind": "toggle", "period_ms": 1000}
    with pytest.raises(
        ValidationError, match="a toggle needs a single or double point"
    ):
        Point.model_validate(_point(kind="float", value=1.0, simulation=simulation))


def test_a_ramp_needs_a_measured_point_and_a_non_zero_increment() -> None:
    with pytest.raises(
        ValidationError, match="needs a normalized, scaled, or float point"
    ):
        Point.model_validate(
            _point(
                kind="single",
                simulation={"kind": "ramp", "period_ms": 10, "increment": 1.0},
            )
        )
    with pytest.raises(ValidationError, match="must not be zero"):
        Point.model_validate(
            _point(
                kind="float",
                value=0.0,
                simulation={"kind": "ramp", "period_ms": 10, "increment": 0},
            )
        )


def test_a_walk_needs_a_positive_size_and_a_scaled_point_an_integer_step() -> None:
    with pytest.raises(ValidationError, match="must be above zero"):
        Point.model_validate(
            _point(
                kind="float",
                value=0.0,
                simulation={"kind": "walk", "period_ms": 10, "increment": -1.0},
            )
        )
    with pytest.raises(ValidationError, match="takes an integer 'increment'"):
        Point.model_validate(
            _point(
                kind="scaled",
                value=0,
                simulation={"kind": "ramp", "period_ms": 10, "increment": 0.5},
            )
        )


def test_a_period_must_be_at_least_one_millisecond() -> None:
    with pytest.raises(ValidationError, match="period_ms"):
        Point.model_validate(
            _point(kind="single", simulation={"kind": "toggle", "period_ms": 0})
        )


def test_an_unknown_field_of_a_point_is_refused_by_name() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Point.model_validate(_point(group=1))
