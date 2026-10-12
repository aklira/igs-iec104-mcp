"""The instance actions against a fake bench (IMPLEMENTATION.md §8; task E5).

The waits for a connection, the transfer, an interrogation that ignores the answers
before its cursor, a stop that waits for its clients to disconnect, and a point update
that waits for the stored value. The fakes script the states and the events, so each
wait can be checked.
"""

import asyncio
from typing import Any

from igs_iec104_mcp.models.run import Outcome, RunStatus
from igs_iec104_mcp.models.scenario import Scenario
from igs_iec104_mcp.scenarios.actions import actions_for
from igs_iec104_mcp.scenarios.engine import Event, Observation, Result, execute

CLIENT = "client-1"
STATION = "server-1"
TERMINATION = {"kind": "asdu", "asdu": {"cause": 10}}


class Idle:
    """A source with no events: the actions here are judged by their control API."""

    async def last_seq(self, instance: str) -> int:
        return 0

    async def events_after(self, instance: str, cursor: int) -> list[Event]:
        return []

    async def image(self, instance: str, address: int) -> Observation | None:
        return None


class FakeControl:
    """A control API whose states and events the test sets."""

    def __init__(
        self, *, connection: str | None = "up", transfer: str = "stopped"
    ) -> None:
        self.state: dict[str, Any] = {"ready": True, "transfer": transfer}
        if connection is not None:
            self.state["connection"] = connection
        self.events_kept: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.points: dict[int, Any] = {}

    async def ready(self) -> dict[str, Any]:
        return dict(self.state)

    async def station_events(self, after: int = 0) -> dict[str, Any]:
        return {"events": [], "last_seq": 0}

    async def events(self, after: int = 0) -> dict[str, Any]:
        kept = [event for event in self.events_kept if event["seq"] > after]
        last = max((event["seq"] for event in self.events_kept), default=0)
        return {"events": kept, "last_seq": last}

    async def start_transfer(self) -> dict[str, Any]:
        self.calls.append("start_transfer")
        self.state["transfer"] = "started"
        return {"accepted": True}

    async def general_interrogation(self) -> dict[str, Any]:
        self.calls.append("general_interrogation")
        return {"accepted": True}

    async def point(self, address: int) -> dict[str, Any]:
        return {"value": self.points.get(address)}

    async def update_point(self, address: int, value: Any) -> dict[str, Any]:
        self.calls.append("update_point")
        self.points[address] = value
        return {"value": value}

    async def image_points(self) -> dict[str, Any]:
        return {
            "points": [
                {"address": address, "value": value}
                for address, value in self.points.items()
            ]
        }


class FakeBench:
    """The controls of the instances. A stop disconnects the clients of the station,
    unless `disconnects` is false."""

    def __init__(
        self,
        controls: dict[str, FakeControl],
        clients: dict[str, list[str]],
        *,
        disconnects: bool = True,
    ) -> None:
        self.controls = controls
        self.clients = clients
        self.disconnects = disconnects
        self.stopped: list[str] = []
        self.started: list[str] = []

    def control(self, instance: str) -> FakeControl:
        return self.controls[instance]

    def clients_of(self, instance: str) -> list[str]:
        return self.clients.get(instance, [])

    async def stop(self, instance: str) -> None:
        self.stopped.append(instance)
        for client in self.clients_of(instance) if self.disconnects else []:
            self.controls[client].state["connection"] = "down"

    async def start(self, instance: str) -> None:
        self.started.append(instance)


def scenario(*steps: dict[str, Any]) -> Scenario:
    return Scenario.model_validate(
        {
            "schema_version": 1,
            "name": "actions",
            "bench": "basic-pair",
            "steps": list(steps),
        }
    )


async def run(bench: FakeBench, *steps: dict[str, Any]) -> Result:
    return await execute(scenario(*steps), actions_for(bench), Idle())


async def test_wait_connection_passes_once_the_client_is_up() -> None:
    client = FakeControl(connection="down")
    bench = FakeBench({CLIENT: client}, {})

    async def connect_later() -> None:
        await asyncio.sleep(0.05)
        client.state["connection"] = "up"

    connecting = asyncio.create_task(connect_later())
    result = await run(
        bench, {"action": "wait_connection", "instance": CLIENT, "timeout": "2s"}
    )
    await connecting
    assert result.status is RunStatus.PASSED
    assert result.verdicts[0].detail == f"{CLIENT} is connected"


async def test_wait_connection_on_a_station_is_an_error_not_a_pass() -> None:
    bench = FakeBench({STATION: FakeControl(connection=None)}, {})
    result = await run(
        bench, {"action": "wait_connection", "instance": STATION, "timeout": "1s"}
    )
    assert result.status is RunStatus.ERROR
    assert result.verdicts[0].outcome is Outcome.ERROR
    assert "wait_connection takes a client" in result.verdicts[0].detail


async def test_start_transfer_requests_it_and_waits_for_it_to_start() -> None:
    client = FakeControl(transfer="stopped")
    bench = FakeBench({CLIENT: client}, {})
    result = await run(bench, {"action": "start_transfer", "client": CLIENT})
    assert result.status is RunStatus.PASSED
    assert client.calls == ["start_transfer"]
    assert result.verdicts[0].detail == f"{CLIENT} transfer is started"


async def test_an_interrogation_ends_on_its_own_termination_not_an_earlier_one() -> (
    None
):
    client = FakeControl()
    client.events_kept.append({"seq": 1, **TERMINATION})  # before the cursor

    async def terminate_later() -> None:
        await asyncio.sleep(0.05)
        client.events_kept.append({"seq": 2, **TERMINATION})

    terminating = asyncio.create_task(terminate_later())
    bench = FakeBench({CLIENT: client}, {})
    result = await run(bench, {"action": "general_interrogation", "client": CLIENT})
    await terminating
    assert result.status is RunStatus.PASSED
    assert result.verdicts[0].detail == (
        f"the interrogation of {CLIENT} ended at seq 2"
    )


async def test_stop_waits_for_the_connected_client_to_disconnect() -> None:
    client = FakeControl(connection="up")
    station = FakeControl()
    bench = FakeBench({CLIENT: client, STATION: station}, {STATION: [CLIENT]})
    result = await run(bench, {"action": "stop_instance", "instance": STATION})
    assert result.status is RunStatus.PASSED
    assert bench.stopped == [STATION]
    assert client.state["connection"] == "down"
    assert result.verdicts[0].detail == (f"{STATION} stopped; 1 client(s) disconnected")


async def test_stop_of_a_station_without_a_connected_client_passes_at_once() -> None:
    client = FakeControl(connection="down")
    station = FakeControl()
    bench = FakeBench({CLIENT: client, STATION: station}, {STATION: [CLIENT]})
    result = await run(bench, {"action": "stop_instance", "instance": STATION})
    assert result.status is RunStatus.PASSED
    assert result.verdicts[0].detail == f"{STATION} stopped; 0 client(s) disconnected"


async def test_a_client_that_never_disconnects_times_the_stop_out() -> None:
    client = FakeControl(connection="up")
    station = FakeControl()
    bench = FakeBench(
        {CLIENT: client, STATION: station}, {STATION: [CLIENT]}, disconnects=False
    )
    result = await run(
        bench,
        {"action": "stop_instance", "instance": STATION, "timeout": "300ms"},
    )
    assert result.status is RunStatus.FAILED
    assert result.verdicts[0].outcome is Outcome.TIMED_OUT
    assert result.verdicts[0].detail == "no result within 0.3 s"


async def test_an_update_passes_once_the_station_stores_the_value() -> None:
    station = FakeControl()
    bench = FakeBench({STATION: station}, {})
    result = await run(
        bench,
        {
            "action": "update_point",
            "server": STATION,
            "address": 100,
            "value": 18.0,
        },
    )
    assert result.status is RunStatus.PASSED
    assert station.points == {100: 18.0}
    assert result.verdicts[0].detail == f"point 100 of {STATION} stores 18.0"


async def test_an_update_waits_until_the_connected_client_holds_the_value() -> None:
    station = FakeControl()
    client = FakeControl()
    bench = FakeBench({STATION: station, CLIENT: client}, {STATION: [CLIENT]})

    async def observe_later() -> None:
        await asyncio.sleep(0.1)
        client.points[100] = 18.0

    observing = asyncio.create_task(observe_later())
    result = await run(
        bench,
        {
            "action": "update_point",
            "server": STATION,
            "address": 100,
            "value": 18.0,
            "timeout": "2s",
        },
    )
    await observing
    assert result.status is RunStatus.PASSED
    assert result.verdicts[0].detail.endswith(f"; {CLIENT} holds 18.0")
