"""The actions that drive the instances of a bench: a client's connection, its transfer,
its interrogation, a station's point update, and the stop and start of an instance
(IMPLEMENTATION.md §8; task E5).

The actions reach each instance through its control API, given by a `Bench`. They wait
on states (the connection and the transfer, as `ready` reports them) and on the events
after a cursor (the end of an interrogation), so an earlier state or answer cannot
satisfy a later step. A stop waits for the clients of the instance to record the
disconnection, so that a later wait for the connection cannot see the connection of
before the stop. Each wait ends at the deadline of its step.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from igs_iec104_mcp.scenarios.engine import Context, Handler, StepFailed

POLL_SECONDS = 0.05
# The cause of transmission that ends an interrogation (IEC 60870-5-104 §8).
CAUSE_TERMINATION = 10


class Control(Protocol):
    """The reads and calls of a control API that the actions use."""

    async def ready(self) -> dict[str, Any]: ...

    async def events(self, after: int = 0) -> dict[str, Any]: ...

    async def station_events(self, after: int = 0) -> dict[str, Any]: ...

    async def start_transfer(self) -> dict[str, Any]: ...

    async def general_interrogation(self) -> dict[str, Any]: ...

    async def point(self, address: int) -> dict[str, Any]: ...

    async def update_point(self, address: int, value: Any) -> dict[str, Any]: ...

    async def image_points(self) -> dict[str, Any]: ...


class Fault(Protocol):
    """The control API of a fault proxy: its state, and its mode."""

    async def state(self) -> dict[str, Any]: ...

    async def set(self, mode: str, latency_ms: int = 0) -> dict[str, Any]: ...


class Bench(Protocol):
    """The instances of a bench: their control APIs, the clients of each station, the
    stations of each client in the order of its group, how each instance is stopped and
    started again, and the fault proxy of each connection that has one."""

    def control(self, instance: str) -> Control: ...

    def clients_of(self, instance: str) -> list[str]: ...

    def stations_of(self, client: str) -> list[str]: ...

    def fault(self, client: str, server: str) -> Fault: ...

    async def stop(self, instance: str) -> None: ...

    async def start(self, instance: str) -> None: ...


@dataclass(frozen=True)
class _Link:
    """A client connected to an instance that a stop takes down. `endpoint` is the
    instance's index in the client's group, None for a client of one station. `started`
    is true when the group had started that station. `cursor` is the client's last event
    before the stop."""

    client: str
    control: Control
    endpoint: int | None
    started: bool
    cursor: int


async def _poll(check: Callable[[], Awaitable[str | None]]) -> str:
    """Repeats the check until it gives a detail; the step's deadline ends the wait."""
    while True:
        detail = await check()
        if detail is not None:
            return detail
        await asyncio.sleep(POLL_SECONDS)


def actions_for(bench: Bench) -> Mapping[str, Handler]:
    """The handlers of the instance actions, over the instances of one bench."""

    async def wait_connection(step: Any, context: Context) -> str:
        instance = step.instance
        control = bench.control(instance)

        async def connected() -> str | None:
            body = await control.ready()
            if "connection" not in body:
                raise ValueError(
                    f"{instance} has no connection of its own: wait_connection takes a "
                    f"client"
                )
            return f"{instance} is connected" if body["connection"] == "up" else None

        return await _poll(connected)

    async def start_transfer(step: Any, context: Context) -> str:
        control = bench.control(step.client)
        await control.start_transfer()
        return await _wait_transfer(control, step.client)

    async def expect_refusal(step: Any, context: Context) -> str:
        """The refusal is seen by the station, which records its security alarm and
        accepts no connection, or by the client, which records that it was refused."""
        client = bench.control(step.client)
        station = bench.control(step.server)
        client_cursor = int((await client.events())["last_seq"])
        station_cursor = int((await station.station_events())["last_seq"])

        async def refused() -> str | None:
            seen = await station.station_events(after=station_cursor)
            if any(event["kind"] == "connected" for event in seen["events"]):
                raise StepFailed(
                    f"{step.server} accepted a connection: the refusal did not happen"
                )
            for event in seen["events"]:
                if event["kind"] == "security" and event["severity"] == "alarm":
                    return f"{step.server} refused the handshake: {event['mnemonic']}"
            for event in (await client.events(after=client_cursor))["events"]:
                if event["kind"] == "connect_failed":
                    return f"{step.client} was refused: {event['error']}"
            return None

        return await _poll(refused)

    async def wait_transfer_started(step: Any, context: Context) -> str:
        return await _wait_transfer(bench.control(step.client), step.client)

    async def general_interrogation(step: Any, context: Context) -> str:
        control = bench.control(step.client)
        cursor = int((await control.events())["last_seq"])
        await control.general_interrogation()

        async def ended() -> str | None:
            body = await control.events(after=cursor)
            for event in body["events"]:
                if (
                    event["kind"] == "asdu"
                    and event["asdu"]["cause"] == CAUSE_TERMINATION
                ):
                    return (
                        f"the interrogation of {step.client} ended at seq "
                        f"{event['seq']}"
                    )
            return None

        return await _poll(ended)

    async def update_point(step: Any, context: Context) -> str:
        """The update is complete when the station stores the value and each client of
        the station holds it in its image. Then a following assertion on the client sees
        the update, not the value before it."""
        control = bench.control(step.server)
        await control.update_point(step.address, step.value)

        async def stored() -> str | None:
            body = await control.point(step.address)
            return (
                f"point {step.address} of {step.server} stores {step.value!r}"
                if body.get("value") == step.value
                else None
            )

        detail = await _poll(stored)
        for client in bench.clients_of(step.server):
            detail += await _wait_image(bench.control(client), client, step)
        return detail

    async def stop_instance(step: Any, context: Context) -> str:
        instance = step.instance
        links = await _links_of(bench, instance)
        await bench.stop(instance)
        for link in links:
            await _wait_lost(link)
        return f"{instance} stopped; {len(links)} client(s) lost it"

    async def switchover(step: Any, context: Context) -> str:
        """Stops the station that a group has started. The group then starts the
        transfer of another connected station, and the step waits for that switchover:
        the `switched` event after the cursor, and the transfer started on the new
        station."""
        control = bench.control(step.client)
        started = (await control.ready()).get("started_endpoint")
        if started is None:
            raise StepFailed(f"{step.client} has no started station to stop")
        stations = bench.stations_of(step.client)
        cursor = int((await control.events())["last_seq"])
        await bench.stop(stations[started])

        async def switched() -> str | None:
            to = await _moved(control, started, cursor)
            if to is None:
                return None
            return (
                f"{step.client} switched from {stations[started]} to "
                f"{stations[to]}; transfer started"
            )

        return await _poll(switched)

    async def start_instance(step: Any, context: Context) -> str:
        await bench.start(step.instance)
        return f"{step.instance} started"

    async def inject_fault(step: Any, context: Context) -> str:
        """An interrupt is complete when the client's connection is down: the proxy
        refuses the reconnections until the fault is cleared. A latency is complete when
        the proxy reports it."""
        link = bench.fault(step.client, step.server)
        if step.mode == "interrupt":
            await link.set("interrupt")
            await _wait_down(bench.control(step.client), step.client)
            return f"{step.client} lost {step.server} through the proxy (interrupt)"
        await link.set("latency", step.latency_ms)
        state = await link.state()
        if state.get("mode") != "latency" or state.get("latency_ms") != step.latency_ms:
            raise StepFailed(f"the proxy reports {state}, not the latency asked for")
        return f"{step.client} -> {step.server}: {step.latency_ms} ms of latency"

    async def clear_fault(step: Any, context: Context) -> str:
        link = bench.fault(step.client, step.server)
        await link.set("none")
        state = await link.state()
        if state.get("mode") != "none":
            raise StepFailed(f"the proxy still reports {state}")
        return f"{step.client} -> {step.server}: no fault"

    return {
        "wait_connection": wait_connection,
        "expect_refusal": expect_refusal,
        "start_transfer": start_transfer,
        "wait_transfer_started": wait_transfer_started,
        "general_interrogation": general_interrogation,
        "update_point": update_point,
        "stop_instance": stop_instance,
        "switchover": switchover,
        "inject_fault": inject_fault,
        "clear_fault": clear_fault,
        "start_instance": start_instance,
    }


async def _links_of(bench: Bench, instance: str) -> list[_Link]:
    """The clients that are connected to an instance, with the cursor of each, before
    the instance is stopped. A group client is connected while any of its stations is,
    so the link to this station is what the step waits for."""
    links: list[_Link] = []
    for client in bench.clients_of(instance):
        control = bench.control(client)
        state = await control.ready()
        if state.get("connection") != "up":
            continue
        stations = bench.stations_of(client)
        endpoint = stations.index(instance) if len(stations) > 1 else None
        started = endpoint is not None and state.get("started_endpoint") == endpoint
        cursor = int((await control.events())["last_seq"])
        links.append(_Link(client, control, endpoint, started, cursor))
    return links


async def _moved(control: Control, endpoint: int, cursor: int) -> int | None:
    """The station a group switched to after the cursor, away from `endpoint`, once the
    transfer is started there. None until then."""
    body = await control.events(after=cursor)
    moved = next(
        (
            event
            for event in body["events"]
            if event["kind"] == "switched" and event["from"] == endpoint
        ),
        None,
    )
    if moved is None:
        return None
    state = await control.ready()
    if state.get("started_endpoint") != moved["to"]:
        return None
    if state.get("transfer") != "started":
        return None
    return int(moved["to"])


async def _wait_lost(link: _Link) -> str:
    """A client of one station is down once its connection is down. A group client
    records the loss of the station's endpoint after the cursor, so an earlier loss
    cannot satisfy the wait. When the lost station was the one the group started, the
    wait ends at the switchover, with the transfer started on the new station, or when
    no station is left connected: the next step then sees the group's state after the
    loss, not before it."""
    if link.endpoint is None:
        return await _wait_down(link.control, link.client)

    endpoint = link.endpoint
    if link.started:

        async def moved_or_down() -> str | None:
            to = await _moved(link.control, endpoint, link.cursor)
            if to is not None:
                return f"{link.client} switched to endpoint {to}"
            state = await link.control.ready()
            if state.get("connection") == "down":
                return f"{link.client} lost every station"
            return None

        return await _poll(moved_or_down)

    async def lost() -> str | None:
        body = await link.control.events(after=link.cursor)
        for event in body["events"]:
            if event["kind"] == "disconnected" and event.get("endpoint") == endpoint:
                return f"{link.client} lost endpoint {endpoint}"
        return None

    return await _poll(lost)


async def _wait_image(control: Control, client: str, step: Any) -> str:
    """Waits until a client's point image holds the value of a point."""

    async def holds() -> str | None:
        body = await control.image_points()
        for point in body["points"]:
            if point["address"] == step.address and point["value"] == step.value:
                return f"; {client} holds {step.value!r}"
        return None

    return await _poll(holds)


async def _wait_transfer(control: Control, client: str) -> str:
    async def started() -> str | None:
        body = await control.ready()
        return (
            f"{client} transfer is started"
            if body.get("transfer") == "started"
            else None
        )

    return await _poll(started)


async def _wait_down(control: Control, client: str) -> str:
    async def down() -> str | None:
        body = await control.ready()
        return f"{client} is disconnected" if body.get("connection") == "down" else None

    return await _poll(down)
