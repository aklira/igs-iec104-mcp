"""The assertions against a fake runtime (IMPLEMENTATION.md §8; task E2).

Each matcher, its tolerance, and its failure message; the point image; the wait for an
expected observation, which never uses one from before its cursor; and a scenario that
runs both kinds.
"""

import asyncio
from typing import Any

import pytest

from igs_iec104_mcp.models.run import Outcome, RunStatus
from igs_iec104_mcp.models.scenario import AssertPoint, ExpectPoint, Scenario
from igs_iec104_mcp.scenarios.assertions import (
    ASSERTION_HANDLERS,
    assert_point,
    expect_point,
    mismatches,
    value_matches,
)
from igs_iec104_mcp.scenarios.engine import (
    Context,
    Event,
    Observation,
    StepFailed,
    execute,
)

CLIENT = "client-1"


def last_of(events: list[Event], instance: str) -> int:
    return max((e.seq for e in events if e.instance == instance), default=0)


class FakeSource:
    """The events of the client, numbered as a runtime numbers them, and its image.

    A careless variant returns every event, old ones too: the engine must
    not match them.
    """

    def __init__(self, careless: bool = False) -> None:
        self.events: list[Event] = []
        self.careless = careless

    def publish(
        self,
        address: int,
        type_id: int,
        value: Any,
        *,
        cause: int = 3,
        common_address: int = 1,
        quality: dict[str, bool] | None = None,
        time: str | None = None,
    ) -> Event:
        data: dict[str, Any] = {
            "address": address,
            "type_id": type_id,
            "value": value,
            "cause": cause,
            "common_address": common_address,
            "quality": quality or {},
        }
        if time is not None:
            data["time"] = time
        event = Event(
            seq=last_of(self.events, CLIENT) + 1,
            instance=CLIENT,
            kind="point",
            data=data,
        )
        self.events.append(event)
        return event

    async def last_seq(self, instance: str) -> int:
        return last_of(self.events, instance)

    async def events_after(self, instance: str, cursor: int) -> list[Event]:
        if self.careless:
            return [e for e in self.events if e.instance == instance]
        return [e for e in self.events if e.instance == instance and e.seq > cursor]

    async def image(self, instance: str, address: int) -> Observation | None:
        found = None
        for event in self.events:
            if event.instance == instance and event.data.get("address") == address:
                found = event
        if found is None:
            return None
        data = found.data
        return Observation(
            seq=found.seq,
            address=address,
            type_id=data["type_id"],
            value=data["value"],
            cause=data["cause"],
            common_address=data["common_address"],
            quality=data["quality"],
            time=data.get("time"),
        )


def expect(**changes: Any) -> ExpectPoint:
    data: dict[str, Any] = {
        "action": "expect_point",
        "client": CLIENT,
        "address": 100,
        "kind": "float",
        "value": 12.5,
        "tolerance": 0.001,
        "timeout": "300ms",
    }
    data.update(changes)
    return ExpectPoint.model_validate(data)


def assertion(**changes: Any) -> AssertPoint:
    data: dict[str, Any] = {
        "action": "assert_point",
        "client": CLIENT,
        "address": 100,
        "kind": "float",
        "value": 12.5,
        "tolerance": 0.001,
    }
    data.update(changes)
    return AssertPoint.model_validate(data)


def observation(**changes: Any) -> Observation:
    fields: dict[str, Any] = {
        "seq": 1,
        "address": 100,
        "type_id": 13,
        "value": 12.5,
        "cause": 3,
        "common_address": 1,
        "quality": {},
        "time": None,
    }
    fields.update(changes)
    return Observation(**fields)


def scenario_with(*steps: Any) -> Scenario:
    return Scenario.model_validate(
        {
            "schema_version": 1,
            "name": "assertions",
            "bench": "basic-pair",
            "steps": [step.model_dump(mode="json") for step in steps],
        }
    )


# The value matcher and its tolerance.


def test_a_number_within_its_tolerance_matches() -> None:
    assert value_matches(12.5, 12.5004, 0.001)
    assert value_matches(12.5, 12.501, 0.001)


def test_a_number_outside_its_tolerance_does_not_match() -> None:
    assert not value_matches(12.5, 12.6, 0.001)


def test_with_no_tolerance_a_number_must_be_exact() -> None:
    assert not value_matches(12.5, 12.5000001, 0.0)
    assert value_matches(12, 12, 0.0)


def test_a_boolean_is_never_a_number() -> None:
    assert value_matches(True, True, 5.0)
    assert not value_matches(True, 1, 5.0)
    assert not value_matches(0, False, 5.0)


def test_an_object_value_must_be_equal() -> None:
    expected = {"value": -2, "transient": True}
    assert value_matches(expected, {"value": -2, "transient": True}, 0.5)
    assert not value_matches(expected, {"value": -2, "transient": False}, 0.5)


def test_a_value_mismatch_names_the_received_expected_value_and_the_tolerance() -> None:
    reasons = mismatches(
        assertion(value=12.5, tolerance=0.001), observation(value=12.6)
    )
    assert reasons == ["the value is 12.6, not 12.5 (tolerance 0.001)"]


def test_a_value_mismatch_without_a_tolerance_names_no_tolerance() -> None:
    reasons = mismatches(
        assertion(kind="single", value=True, tolerance=0.0),
        observation(type_id=1, value=False),
    )
    assert reasons == ["the value is False, not True"]


# The kind of the point, from its type.


def test_the_kind_of_the_point_must_be_the_kind_of_its_type() -> None:
    reasons = mismatches(
        expect(kind="double", value="on", tolerance=0.0),
        observation(type_id=13, value="on"),
    )
    assert reasons == ["the point is a float point, not a double point"]


def test_an_unknown_type_is_named_by_its_number() -> None:
    reasons = mismatches(assertion(), observation(type_id=99))
    assert reasons == ["the point is a type 99 point, not a float point"]


# The common address, the cause, and the time tag.


def test_a_common_address_is_checked_only_when_named() -> None:
    assert mismatches(assertion(), observation(common_address=7)) == []
    reasons = mismatches(assertion(common_address=1), observation(common_address=7))
    assert reasons == ["the common address is 7, not 1"]


def test_a_cause_is_checked_only_when_named() -> None:
    assert mismatches(assertion(), observation(cause=20)) == []
    assert mismatches(assertion(cause=3), observation(cause=20)) == [
        "the cause is 20, not 3"
    ]


def test_a_time_tag_is_checked_only_when_named() -> None:
    assert mismatches(assertion(), observation(time="2026-10-11T12:00:00Z")) == []
    assert mismatches(assertion(stamped=True), observation(time=None)) == [
        "the observation has no time tag, but the step expects one"
    ]
    assert mismatches(
        assertion(stamped=False), observation(time="2026-10-11T12:00:00Z")
    ) == ["the observation has a time tag, but the step expects none"]


# The quality flags.


def test_a_named_quality_flag_is_checked_and_an_unnamed_one_is_not() -> None:
    step = assertion(quality={"invalid": False})
    assert mismatches(step, observation(quality={"blocked": True})) == []
    assert mismatches(step, observation(quality={"invalid": True})) == [
        "the quality flag invalid is True, not False"
    ]


def test_a_missing_quality_flag_counts_as_not_set() -> None:
    assert mismatches(assertion(quality={"blocked": False}), observation()) == []


def test_every_condition_of_a_step_gives_its_own_message() -> None:
    step = assertion(value=1.0, cause=3, common_address=2)
    reasons = mismatches(step, observation(value=2.0, cause=20, common_address=1))
    assert len(reasons) == 3


# assert_point reads the point image as it stands.


async def test_assert_point_passes_on_the_value_the_image_holds() -> None:
    source = FakeSource()
    source.publish(100, 13, 12.5)
    assert await assert_point(assertion(), Context(source)) == "point 100 is 12.5"


async def test_assert_point_fails_when_the_point_has_received_no_value() -> None:
    with pytest.raises(StepFailed, match="has not received one"):
        await assert_point(assertion(), Context(FakeSource()))


async def test_assert_point_reports_every_mismatch_at_once() -> None:
    source = FakeSource()
    source.publish(100, 13, 12.9, cause=20)
    with pytest.raises(StepFailed) as error:
        await assert_point(assertion(cause=3), Context(source))
    assert "the value is 12.9, not 12.5 (tolerance 0.001)" in str(error.value)
    assert "the cause is 20, not 3" in str(error.value)


# expect_point waits for an observation after its cursor.


async def test_expect_point_takes_an_observation_after_its_cursor() -> None:
    source = FakeSource()

    async def publish_later() -> None:
        await asyncio.sleep(0.05)
        source.publish(100, 13, 18.0)
        source.publish(100, 13, 12.5)

    publisher = asyncio.create_task(publish_later())
    result = await expect_point(expect(), Context(source))
    await publisher
    assert result == "point 100 observed at seq 2"


async def test_expect_point_never_uses_an_observation_from_before_its_cursor() -> None:
    source = FakeSource(careless=True)
    source.publish(100, 13, 12.5)  # seq 1: it matches, but it is before the cursor

    async def publish_later() -> None:
        await asyncio.sleep(0.05)
        source.publish(100, 13, 12.5)  # seq 2

    publisher = asyncio.create_task(publish_later())
    result = await expect_point(expect(), Context(source))
    await publisher
    assert result == "point 100 observed at seq 2"


async def test_expect_point_times_out_with_what_the_last_observation_lacked() -> None:
    source = FakeSource()

    async def publish_wrong_value() -> None:
        await asyncio.sleep(0.05)
        source.publish(100, 13, 12.5)

    publisher = asyncio.create_task(publish_wrong_value())
    result = await execute(
        scenario_with(expect(value=99.0, timeout="200ms")),
        ASSERTION_HANDLERS,
        source,
    )
    await publisher
    assert [v.outcome for v in result.verdicts] == [Outcome.TIMED_OUT]
    assert result.status is RunStatus.FAILED
    detail = result.verdicts[0].detail
    assert "no result within 0.2 s" in detail
    assert "the value is 12.5, not 99.0 (tolerance 0.001)" in detail


# A scenario that runs both kinds.


async def test_a_scenario_of_assertions_passes_and_stops_at_the_first_failure() -> None:
    source = FakeSource()
    source.publish(100, 13, 12.5)

    async def publish_later() -> None:
        await asyncio.sleep(0.05)
        source.publish(100, 13, 12.5)

    publisher = asyncio.create_task(publish_later())
    passing = scenario_with(assertion(), expect(value=12.5, timeout="500ms"))
    result = await execute(passing, ASSERTION_HANDLERS, source)
    await publisher
    assert result.status is RunStatus.PASSED
    assert [v.outcome for v in result.verdicts] == [Outcome.PASSED, Outcome.PASSED]
    assert result.verdicts[1].detail == "point 100 observed at seq 2"

    failing = scenario_with(assertion(value=99.0), expect(value=12.5, timeout="200ms"))
    result = await execute(failing, ASSERTION_HANDLERS, source)
    assert result.status is RunStatus.FAILED
    assert [v.outcome for v in result.verdicts] == [Outcome.FAILED]
    assert "the value is 12.5, not 99.0" in result.verdicts[0].detail


def test_the_handlers_are_the_two_assertion_actions() -> None:
    assert set(ASSERTION_HANDLERS) == {"assert_point", "expect_point"}
