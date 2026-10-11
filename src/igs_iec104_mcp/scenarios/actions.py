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
from typing import Any, Protocol

from igs_iec104_mcp.scenarios.engine import Context, Handler

POLL_SECONDS = 0.05
# The cause of transmission that ends an interrogation (IEC 60870-5-104 §8).
CAUSE_TERMINATION = 10


class Control(Protocol):
    """The reads and calls of a control API that the actions use."""

    async def ready(self) -> dict[str, Any]: ...

    async def events(self, after: int = 0) -> dict[str, Any]: ...

    async def start_transfer(self) -> dict[str, Any]: ...

    async def general_interrogation(self) -> dict[str, Any]: ...

    async def point(self, address: int) -> dict[str, Any]: ...

    async def update_point(self, address: int, value: Any) -> dict[str, Any]: ...


class Bench(Protocol):
    """The instances of a bench: their control APIs, the clients of each station, and
    how each instance is stopped and started again."""

    def control(self, instance: str) -> Control: ...

    def clients_of(self, instance: str) -> list[str]: ...

    async def stop(self, instance: str) -> None: ...

    async def start(self, instance: str) -> None: ...


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
        control = bench.control(step.server)
        await control.update_point(step.address, step.value)

        async def stored() -> str | None:
            body = await control.point(step.address)
            return (
                f"point {step.address} of {step.server} stores {step.value!r}"
                if body.get("value") == step.value
                else None
            )

        return await _poll(stored)

    async def stop_instance(step: Any, context: Context) -> str:
        instance = step.instance
        connected = [
            client
            for client in bench.clients_of(instance)
            if (await bench.control(client).ready()).get("connection") == "up"
        ]
        await bench.stop(instance)
        for client in connected:
            await _wait_down(bench.control(client), client)
        return f"{instance} stopped; {len(connected)} client(s) disconnected"

    async def start_instance(step: Any, context: Context) -> str:
        await bench.start(step.instance)
        return f"{step.instance} started"

    return {
        "wait_connection": wait_connection,
        "start_transfer": start_transfer,
        "wait_transfer_started": wait_transfer_started,
        "general_interrogation": general_interrogation,
        "update_point": update_point,
        "stop_instance": stop_instance,
        "start_instance": start_instance,
    }


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
