"""The refused connection as a scenario step (IMPLEMENTATION.md §7, §8; task V1c).

A step that expects a refusal passes when the station records a security alarm for the
handshake, or the client records that it was refused, and the station accepts no
connection meanwhile. A station that accepts the connection fails the step. The check
names a client and the station it connects to.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from test_scenario_runner import BENCH

from igs_iec104_mcp.models.common import DefinitionError
from igs_iec104_mcp.models.loader import load_bench
from igs_iec104_mcp.models.scenario import Scenario, check_scenario
from igs_iec104_mcp.runtime.source import ControlSource
from igs_iec104_mcp.scenarios.actions import actions_for
from igs_iec104_mcp.scenarios.engine import Context, StepFailed


def scenario(client: str = "client-1", server: str = "server-1") -> Scenario:
    return Scenario.model_validate(
        {
            "schema_version": 1,
            "name": "refusal",
            "bench": "basic-pair",
            "steps": [
                {
                    "action": "expect_refusal",
                    "client": client,
                    "server": server,
                    "timeout": "5s",
                }
            ],
        }
    )


def bench() -> Any:
    return load_bench(BENCH)


def test_a_refusal_names_a_client_and_the_station_it_connects_to() -> None:
    check_scenario(scenario(), bench())


@pytest.mark.parametrize(
    ("client", "server", "message"),
    [
        ("client-9", "server-1", "not an instance of the bench"),
        ("client-1", "client-1", "is a client: a refusal is seen by a station"),
        ("server-1", "server-1", "is a server, and only a client has an image"),
    ],
)
def test_a_refusal_with_a_wrong_name_or_role_is_refused(
    client: str, server: str, message: str
) -> None:
    with pytest.raises(DefinitionError, match=message):
        check_scenario(scenario(client=client, server=server), bench())


class FakeControl:
    """A control API with the events the test gives it."""

    def __init__(
        self, events: list[dict[str, Any]], station: list[dict[str, Any]]
    ) -> None:
        self.events_list = events
        self.station_list = station

    async def ready(self) -> dict[str, Any]:
        return {"connection": "down"}

    async def events(self, after: int = 0) -> dict[str, Any]:
        kept = [e for e in self.events_list if e["seq"] > after]
        return {"events": kept, "last_seq": len(self.events_list)}

    async def station_events(self, after: int = 0) -> dict[str, Any]:
        kept = [e for e in self.station_list if e["seq"] > after]
        return {"events": kept, "last_seq": len(self.station_list)}


class FakeBench:
    def __init__(self, client: FakeControl, station: FakeControl) -> None:
        self._controls = {"client-1": client, "server-1": station}

    def control(self, instance: str) -> Any:
        return self._controls[instance]

    def clients_of(self, instance: str) -> list[str]:
        return ["client-1"]

    def stations_of(self, client: str) -> list[str]:
        return ["server-1"]

    async def stop(self, instance: str) -> None:
        return None

    async def start(self, instance: str) -> None:
        return None


def alarm(seq: int) -> dict[str, Any]:
    return {
        "seq": seq,
        "kind": "security",
        "severity": "alarm",
        "mnemonic": "TLS_NO_CA_MATCH",
        "identifier": "IEC 62351-3:2.2",
        "detail": "the chain has no trust anchor",
        "observed_at_ms": 0,
    }


def start(client: FakeControl, station: FakeControl) -> "asyncio.Task[str]":
    """The refusal step, started as the engine starts a step."""
    handler = actions_for(FakeBench(client, station))["expect_refusal"]
    step = scenario().steps[0]

    async def call() -> str:
        return await handler(step, Context(ControlSource({})))

    return asyncio.create_task(call())


async def run_after(
    client: FakeControl, station: FakeControl, arrives: Callable[[], None]
) -> str:
    """The step, with an event that arrives after its cursor, as a station would record
    one while the step waits."""
    task = start(client, station)
    await asyncio.sleep(0.02)
    arrives()
    async with asyncio.timeout(5):
        return await task


async def test_a_station_alarm_passes_the_refusal() -> None:
    station = FakeControl([], [])
    client = FakeControl([], [])
    detail = await run_after(
        client, station, lambda: station.station_list.append(alarm(1))
    )
    assert detail == "server-1 refused the handshake: TLS_NO_CA_MATCH"


async def test_a_refused_client_passes_the_refusal() -> None:
    station = FakeControl([], [])
    client = FakeControl([], [])
    detail = await run_after(
        client,
        station,
        lambda: client.events_list.append(
            {"seq": 1, "kind": "connect_failed", "error": "the handshake failed"}
        ),
    )
    assert detail == "client-1 was refused: the handshake failed"


async def test_a_station_that_accepts_a_connection_fails_the_refusal() -> None:
    station = FakeControl([], [])
    client = FakeControl([], [])

    def accepted() -> None:
        station.station_list.append(
            {"seq": 1, "kind": "connected", "connection": 1, "peer": "x"}
        )

    with pytest.raises(StepFailed, match="accepted a connection"):
        await run_after(client, station, accepted)


async def test_an_event_before_the_step_does_not_pass_it() -> None:
    station = FakeControl([], [alarm(1)])
    client = FakeControl([], [])
    task = start(client, station)
    await asyncio.sleep(0.1)
    assert not task.done(), "the alarm before the step's cursor is not its evidence"
    task.cancel()


def test_a_refusal_scenario_starts_its_bench_without_waiting() -> None:
    from igs_iec104_mcp.models.scenario import Scenario
    from igs_iec104_mcp.scenarios.runner import _expects_refusal

    assert _expects_refusal(scenario().steps)
    plain = Scenario.model_validate(
        {
            "schema_version": 1,
            "name": "plain",
            "bench": "basic-pair",
            "steps": [
                {
                    "action": "parallel",
                    "branches": [
                        [{"action": "wait_connection", "instance": "client-1"}],
                        [{"action": "general_interrogation", "client": "client-1"}],
                    ],
                }
            ],
        }
    )
    assert not _expects_refusal(plain.steps)
    nested = Scenario.model_validate(
        {
            "schema_version": 1,
            "name": "nested",
            "bench": "basic-pair",
            "steps": [
                {
                    "action": "parallel",
                    "branches": [
                        [
                            {
                                "action": "expect_refusal",
                                "client": "client-1",
                                "server": "server-1",
                                "timeout": "5s",
                            }
                        ]
                    ],
                }
            ],
        }
    )
    assert _expects_refusal(nested.steps)
