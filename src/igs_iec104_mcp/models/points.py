"""
The points of a station: their kind, their value, their time tag, their command policy,
and their simulation (IMPLEMENTATION.md §5).

The kinds are the monitoring types of the upstream `PointValue` (`igs-iec104`,
process_image.rs). The kinds with no time-tagged form are the packed single-point (M_PS)
and the normalized value without quality (M_ME_ND), as the upstream documents them.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from igs_iec104_mcp.models.common import Address, Definition

# Upper bound of a simulation period: one day.
MAX_PERIOD_MS: int = 86_400_000
U16_MAX: int = 65_535
U32_MAX: int = 4_294_967_295
I16_MIN, I16_MAX = -32_768, 32_767
I32_MIN, I32_MAX = -2_147_483_648, 2_147_483_647
NORMALIZED_GRID: int = 32_768


class PointKind(StrEnum):
    """The monitoring kinds of a point, as the upstream `PointValue` names them."""

    SINGLE = "single"
    DOUBLE = "double"
    STEP = "step"
    BITSTRING = "bitstring"
    NORMALIZED = "normalized"
    SCALED = "scaled"
    FLOAT = "float"
    COUNTER = "counter"
    PACKED = "packed"
    NORMALIZED_UNQUALIFIED = "normalized_unqualified"


# The kinds that have a time-tagged form (the kinds not listed here take no time tag).
TIME_TAGGED_KINDS: frozenset[PointKind] = frozenset(PointKind) - {
    PointKind.PACKED,
    PointKind.NORMALIZED_UNQUALIFIED,
}
SINGLE_OR_DOUBLE: frozenset[PointKind] = frozenset({PointKind.SINGLE, PointKind.DOUBLE})
MEASURED: frozenset[PointKind] = frozenset(
    {PointKind.NORMALIZED, PointKind.SCALED, PointKind.FLOAT}
)
TOGGLE_KINDS: frozenset[PointKind] = SINGLE_OR_DOUBLE
RAMP_KINDS: frozenset[PointKind] = MEASURED


class CommandPolicy(StrEnum):
    """How the station answers the commands to a point (§7)."""

    UNKNOWN_ADDRESS = "unknown_address"
    REFUSE = "refuse"
    ACCEPT = "accept"


class SimulationKind(StrEnum):
    """How a point changes by itself: toggle, ramp, or a seeded walk."""

    TOGGLE = "toggle"
    RAMP = "ramp"
    WALK = "walk"


class Simulation(Definition):
    """A change of a point at each period. Every change is a function of the step and"
    "the seed."""

    kind: SimulationKind
    period_ms: int = Field(
        ge=1,
        le=MAX_PERIOD_MS,
        description="The time between two steps, in milliseconds.",
    )
    increment: float | None = Field(
        default=None,
        description=(
            "The change at each step, for ramp (any sign) and walk (its size, above "
            "zero). Not used by toggle."
        ),
    )
    seed: int = Field(default=0, ge=0, le=2**64 - 1, description="The seed of a walk.")


class Point(Definition):
    """One information object of a station, with its value and its behaviour."""

    address: Address
    kind: PointKind
    value: Any = Field(
        description="The value, in the form of the kind (see PointKind)."
    )
    stamped: bool = False
    command: CommandPolicy = CommandPolicy.UNKNOWN_ADDRESS
    simulation: Simulation | None = None

    @model_validator(mode="after")
    def _check_point(self) -> Point:
        where = f"point {self.address}"
        check_value(self.kind, self.value, where)
        if self.stamped and self.kind not in TIME_TAGGED_KINDS:
            raise ValueError(
                f"{where}: a {self.kind} point has no time-tagged form, so 'stamped' "
                f"must be false"
            )
        if self.command == CommandPolicy.ACCEPT and self.kind not in SINGLE_OR_DOUBLE:
            raise ValueError(
                f"{where}: a command can be accepted only on a single or double point; "
                "use 'refuse' or 'unknown_address' for this kind"
            )
        if self.simulation is not None:
            check_simulation(self.kind, self.simulation, where)
        return self


def check_value(kind: PointKind, value: Any, where: str) -> None:
    """Raises ValueError, naming the form the kind takes, when `value` does not fit "
    "`kind`."""
    if kind is PointKind.SINGLE:
        ok = isinstance(value, bool)
        expected = "true or false"
    elif kind is PointKind.DOUBLE:
        ok = value in {"off", "on", "intermediate", "indeterminate"}
        expected = 'one of "off", "on", "intermediate", "indeterminate"'
    elif kind is PointKind.STEP:
        ok = _is_step(value)
        expected = "an object with 'value' from -64 to 63 and 'transient' true or false"
    elif kind is PointKind.BITSTRING:
        ok = _is_int(value, 0, U32_MAX)
        expected = f"an integer from 0 to {U32_MAX}"
    elif kind in {PointKind.NORMALIZED, PointKind.NORMALIZED_UNQUALIFIED}:
        ok = _is_normalized(value)
        expected = "a number from -1 up to 1 (excluding 1), on a grid of 2^-15"
    elif kind is PointKind.SCALED:
        ok = _is_int(value, I16_MIN, I16_MAX)
        expected = f"an integer from {I16_MIN} to {I16_MAX}"
    elif kind is PointKind.FLOAT:
        ok = _is_number(value) and math.isfinite(float(value))
        expected = "a finite number"
    elif kind is PointKind.COUNTER:
        ok = _is_int(value, I32_MIN, I32_MAX)
        expected = f"an integer from {I32_MIN} to {I32_MAX}"
    else:
        ok = _is_packed(value)
        expected = (
            "an object with 'status' and 'change', each an integer from 0 to 65535"
        )
    if not ok:
        raise ValueError(f"{where}: a {kind} point takes {expected}, not {value!r}")


def check_simulation(kind: PointKind, simulation: Simulation, where: str) -> None:
    """
    Raises ValueError when the simulation does not fit the kind, or its parameters are
        inconsistent.
    """
    if simulation.kind is SimulationKind.TOGGLE and kind not in TOGGLE_KINDS:
        raise ValueError(
            f"{where}: a toggle needs a single or double point, not a {kind} point"
        )
    if (
        simulation.kind in {SimulationKind.RAMP, SimulationKind.WALK}
        and kind not in RAMP_KINDS
    ):
        raise ValueError(
            f"{where}: a {simulation.kind} needs a normalized, scaled, or float "
            f"point, not a {kind} point"
        )
    if simulation.kind is SimulationKind.TOGGLE:
        return
    increment = simulation.increment
    if increment is None or not math.isfinite(increment):
        raise ValueError(f"{where}: a {simulation.kind} needs a finite 'increment'")
    if simulation.kind is SimulationKind.WALK and increment <= 0:
        raise ValueError(
            f"{where}: the walk's 'increment' is its size, so it must be above zero"
        )
    if simulation.kind is SimulationKind.RAMP and increment == 0:
        raise ValueError(
            f"{where}: a ramp's 'increment' must not be zero, or the value never moves"
        )
    if kind is PointKind.SCALED and not float(increment).is_integer():
        raise ValueError(f"{where}: a scaled point takes an integer 'increment'")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any, low: int, high: int) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    )


def _is_normalized(value: Any) -> bool:
    if not _is_number(value) or not math.isfinite(float(value)):
        return False
    scaled = float(value) * NORMALIZED_GRID
    return -NORMALIZED_GRID <= scaled < NORMALIZED_GRID and scaled.is_integer()


def _is_step(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"value", "transient"}
        and _is_int(value["value"], -64, 63)
        and isinstance(value["transient"], bool)
    )


def _is_packed(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"status", "change"}
        and _is_int(value["status"], 0, U16_MAX)
        and _is_int(value["change"], 0, U16_MAX)
    )
