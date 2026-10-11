"""The scenario source over a control API, against a fake client (IMPLEMENTATION.md §8;
task E2b): an event keeps its sequence number and its fields, an image point has no
sequence number, the events after a cursor are asked of the runtime, and an unknown
instance is refused."""

from typing import Any

import pytest

from igs_iec104_mcp.runtime.source import (
    ControlSource,
    event_of,
    observation_of_image,
)
from igs_iec104_mcp.scenarios.assertions import observation_of

POINT_EVENT: dict[str, Any] = {
    "seq": 7,
    "observed_at_ms": 1_700_000_000_000,
    "generation": 2,
    "kind": "point",
    "address": 100,
    "type_id": 36,
    "value": 18.0,
    "cause": 3,
    "common_address": 1,
    "quality": {
        "invalid": False,
        "not_topical": False,
        "substituted": False,
        "blocked": False,
        "overflow": False,
    },
    "time": None,
}

POINT_IMAGE: dict[str, Any] = {
    "address": 100,
    "type_id": 36,
    "value": 18.0,
    "quality": POINT_EVENT["quality"],
    "time": None,
    "cause": 3,
    "common_address": 1,
    "observed_at_ms": 1_700_000_000_000,
    "generation": 2,
}


class FakeClient:
    """A control API that answers with the events and the image it holds."""

    def __init__(self) -> None:
        self.events_asked: list[int] = []
        self.events_kept: list[dict[str, Any]] = [POINT_EVENT]
        self.points: list[dict[str, Any]] = [POINT_IMAGE]

    async def events(self, after: int = 0) -> dict[str, Any]:
        self.events_asked.append(after)
        kept = [event for event in self.events_kept if event["seq"] > after]
        return {"events": kept, "last_seq": POINT_EVENT["seq"]}

    async def image_points(self) -> dict[str, Any]:
        return {"points": self.points}


def test_an_event_keeps_its_sequence_number_and_its_fields() -> None:
    event = event_of("client-1", POINT_EVENT)
    assert event.seq == 7
    assert event.instance == "client-1"
    assert event.kind == "point"
    assert "seq" not in event.data
    assert "kind" not in event.data
    assert event.data["value"] == 18.0


def test_an_image_point_has_no_sequence_number() -> None:
    observation = observation_of_image(POINT_IMAGE)
    assert observation.seq is None
    assert observation.address == 100
    assert observation.common_address == 1
    assert observation.cause == 3


def test_an_event_and_an_image_point_decode_to_the_same_observation() -> None:
    from_event = observation_of(event_of("client-1", POINT_EVENT))
    from_image = observation_of_image(POINT_IMAGE)
    assert from_event.seq == 7
    assert (from_event.address, from_event.type_id, from_event.value) == (
        from_image.address,
        from_image.type_id,
        from_image.value,
    )
    assert from_event.quality == from_image.quality
    assert from_event.common_address == from_image.common_address


async def test_the_source_asks_the_runtime_for_the_events_after_the_cursor() -> None:
    client = FakeClient()
    source = ControlSource({"client-1": client})
    assert await source.last_seq("client-1") == 7
    assert [e.seq for e in await source.events_after("client-1", 3)] == [7]
    assert await source.events_after("client-1", 7) == []
    assert client.events_asked == [0, 3, 7]


async def test_the_source_finds_a_point_in_the_image_by_its_address() -> None:
    source = ControlSource({"client-1": FakeClient()})
    found = await source.image("client-1", 100)
    assert found is not None
    assert found.value == 18.0
    assert await source.image("client-1", 101) is None


async def test_an_instance_the_source_does_not_know_is_refused() -> None:
    source = ControlSource({"client-1": FakeClient()})
    with pytest.raises(KeyError, match="client-2"):
        await source.last_seq("client-2")
