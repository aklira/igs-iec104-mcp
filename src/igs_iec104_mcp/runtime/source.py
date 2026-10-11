"""The scenario source over the control APIs of the client runtimes.

Task E2b (IMPLEMENTATION.md §8). The events of a client are its contract events
(events.schema.json): each keeps its sequence number, and each point value the client
received is a `point` event of its own, recorded right after the ASDU that carried it.
The point image comes from the runtime's image route (task C2). The source names no
runtime itself: the caller maps each instance of the scenario to the control client of
its runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from igs_iec104_mcp.scenarios.engine import Event, Observation

# The fields of a recorded event that are not part of its data.
_HEADER: frozenset[str] = frozenset({"seq", "kind"})


class ClientApi(Protocol):
    """The reads of a control API that the source uses."""

    async def events(self, after: int = 0) -> dict[str, Any]: ...

    async def image_points(self) -> dict[str, Any]: ...


class ControlSource:
    """The events and the point images of the client runtimes, read through their
    control APIs.

    An instance the source does not know is a KeyError, which the engine reports as an
    error of the step.
    """

    def __init__(self, clients: Mapping[str, ClientApi]) -> None:
        self._clients = dict(clients)

    def _client(self, instance: str) -> ClientApi:
        client = self._clients.get(instance)
        if client is None:
            raise KeyError(f"no runtime is named {instance!r}")
        return client

    async def last_seq(self, instance: str) -> int:
        body = await self._client(instance).events()
        return int(body["last_seq"])

    async def events_after(self, instance: str, cursor: int) -> list[Event]:
        body = await self._client(instance).events(after=cursor)
        return [event_of(instance, recorded) for recorded in body["events"]]

    async def image(self, instance: str, address: int) -> Observation | None:
        body = await self._client(instance).image_points()
        for point in body["points"]:
            if int(point["address"]) == address:
                return observation_of_image(point)
        return None


def event_of(instance: str, recorded: Mapping[str, Any]) -> Event:
    """The engine's event of a recorded event: its sequence number, its kind, and the
    fields that follow them."""
    data = {key: value for key, value in recorded.items() if key not in _HEADER}
    return Event(
        seq=int(recorded["seq"]),
        instance=instance,
        kind=str(recorded["kind"]),
        data=data,
    )


def observation_of_image(point: Mapping[str, Any]) -> Observation:
    """The observation of a point of the image. The image keeps no sequence number."""
    return Observation(
        seq=None,
        address=int(point["address"]),
        type_id=int(point["type_id"]),
        value=point["value"],
        cause=int(point["cause"]),
        common_address=int(point["common_address"]),
        quality=dict(point["quality"]),
        time=point["time"],
    )
