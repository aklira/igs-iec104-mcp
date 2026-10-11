"""The assertions of a scenario (IMPLEMENTATION.md §8; task E2).

The matchers compare a value a client received with what a step expects, each with its
tolerance and its message, and the handlers run the two assertion actions. A matcher
names what differs: the kind of the point, its value, its cause, its common address, its
time tag, and each quality flag the step names. A step that names nothing about an
observation does not check it. The messages say what was received and what was expected,
so that a failed verdict can be read without the runtime's logs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from igs_iec104_mcp.models.scenario import AssertPoint, ExpectPoint, Step
from igs_iec104_mcp.scenarios.engine import (
    Context,
    Event,
    Handler,
    Observation,
    StepFailed,
)

# The point kind of each monitoring type, and the types that carry a time tag (IEC
# 60870-5-104 §8; the types of the upstream `PointValue`).
KIND_OF_TYPE: Mapping[int, str] = {
    1: "single",
    3: "double",
    5: "step",
    7: "bitstring",
    9: "normalized",
    11: "scaled",
    13: "float",
    15: "counter",
    20: "packed",
    21: "normalized_unqualified",
    30: "single",
    31: "double",
    32: "step",
    33: "bitstring",
    34: "normalized",
    35: "scaled",
    36: "float",
    37: "counter",
}
QUALITY_FLAGS: tuple[str, ...] = (
    "invalid",
    "not_topical",
    "substituted",
    "blocked",
    "overflow",
)
POINT_EVENT = "point"


def observation_of(event: Event) -> Observation:
    """The observation a point event of a runtime carries."""
    data = event.data
    return Observation(
        seq=event.seq,
        address=int(data["address"]),
        type_id=int(data["type_id"]),
        value=data["value"],
        cause=int(data["cause"]),
        common_address=int(data["common_address"]),
        quality=dict(data.get("quality", {})),
        time=data.get("time"),
    )


def value_matches(expected: Any, actual: Any, tolerance: float) -> bool:
    """Whether a value is the expected one.

    A number is within the tolerance of the expected number. A boolean, a text, or an
    object must be equal.
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        both = isinstance(expected, bool) and isinstance(actual, bool)
        return both and expected == actual
    if _is_number(expected) and _is_number(actual):
        return abs(float(actual) - float(expected)) <= tolerance
    return bool(expected == actual)


def mismatches(step: AssertPoint | ExpectPoint, observation: Observation) -> list[str]:
    """Every condition of the step that the observation does not meet, as a message.

    An empty list means the observation matches.
    """
    reasons: list[str] = []
    received_kind = KIND_OF_TYPE.get(observation.type_id)
    if received_kind != step.kind:
        received = received_kind or f"type {observation.type_id}"
        reasons.append(f"the point is a {received} point, not a {step.kind} point")
    if not value_matches(step.value, observation.value, step.tolerance):
        tolerance = ""
        if step.tolerance and _is_number(step.value):
            tolerance = f" (tolerance {step.tolerance:g})"
        reasons.append(
            f"the value is {observation.value!r}, not {step.value!r}{tolerance}"
        )
    if step.common_address is not None and observation.common_address != (
        step.common_address
    ):
        reasons.append(
            f"the common address is {observation.common_address}, "
            f"not {step.common_address}"
        )
    if step.cause is not None and observation.cause != step.cause:
        reasons.append(f"the cause is {observation.cause}, not {step.cause}")
    if step.stamped is not None:
        carries = observation.time is not None
        if carries != step.stamped:
            received = "has a time tag" if carries else "has no time tag"
            expects = "one" if step.stamped else "none"
            reasons.append(
                f"the observation {received}, but the step expects {expects}"
            )
    if step.quality is not None:
        for flag in QUALITY_FLAGS:
            wanted = getattr(step.quality, flag)
            received_flag = bool(observation.quality.get(flag, False))
            if wanted is not None and received_flag != wanted:
                reasons.append(
                    f"the quality flag {flag} is {received_flag}, not {wanted}"
                )
    return reasons


async def assert_point(step: Step, context: Context) -> str:
    """Checks the client's point image as it stands.

    The value at the address must meet every condition of the step.
    """
    if not isinstance(step, AssertPoint):
        raise TypeError("assert_point needs an assert_point step")
    observation = await context.image(step.client, step.address)
    if observation is None:
        raise StepFailed(
            f"{step.client} has no value of point {step.address}: "
            "it has not received one"
        )
    reasons = mismatches(step, observation)
    if reasons:
        raise StepFailed("; ".join(reasons))
    return f"point {step.address} is {observation.value!r}"


async def expect_point(step: Step, context: Context) -> str:
    """Waits for an observation of the point after the step's cursor.

    The observation must meet every condition of the step. An observation before the
    cursor is never used. When the deadline passes, the verdict says what the last
    observation lacked.
    """
    if not isinstance(step, ExpectPoint):
        raise TypeError("expect_point needs an expect_point step")
    cursor = await context.cursor(step.client)
    context.note = "no observation of the point after the step started"

    def matches(event: Event) -> bool:
        if event.kind != POINT_EVENT or event.data.get("address") != step.address:
            return False
        reasons = mismatches(step, observation_of(event))
        if reasons:
            context.note = "; ".join(reasons)
            return False
        return True

    event = await context.wait_for(step.client, cursor, matches)
    return f"point {step.address} observed at seq {event.seq}"


ASSERTION_HANDLERS: Mapping[str, Handler] = {
    "assert_point": assert_point,
    "expect_point": expect_point,
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
