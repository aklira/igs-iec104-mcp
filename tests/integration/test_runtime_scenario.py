"""An assertion scenario against a station and a client of the runtime.

Task E2b (IMPLEMENTATION.md §8). Both runtimes run as local processes, as in
milestone 1, and the scenario engine reads the client through its control API.

The runtime binary is named by IGS_RUNTIME_BINARY (see conftest.py).
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from igs_iec104_mcp.models.run import Outcome, RunStatus
from igs_iec104_mcp.models.scenario import Scenario
from igs_iec104_mcp.runtime.api import ControlClient
from igs_iec104_mcp.runtime.process import (
    LocalRuntime,
    read_control_token,
    write_control_token,
)
from igs_iec104_mcp.runtime.source import ControlSource
from igs_iec104_mcp.scenarios.assertions import ASSERTION_HANDLERS
from igs_iec104_mcp.scenarios.engine import Result, execute

CLIENT = "client-1"
ADDRESS = 100
INITIAL = 12.5
UPDATED = 18.0
REJECTED = 25.0
# The cause of the interrogation answers, and of a spontaneous update (IEC 60870-5-104
# §8).
CAUSE_INTERROGATED = 20
CAUSE_SPONTANEOUS = 3
PATIENCE = 10.0
POLL = 0.05
# The station's update comes this long after a scenario starts, so that the cursor of
# the first expectation is taken before it: an earlier point is never matched.
UPDATE_DELAY = 0.5
POINTS: dict[str, Any] = {
    "schema_version": 1,
    "points": [{"address": ADDRESS, "kind": "float", "value": INITIAL}],
}


async def until[T](check: Callable[[], Awaitable[T | None]], what: str) -> T:
    try:
        async with asyncio.timeout(PATIENCE):
            while True:
                value = await check()
                if value is not None:
                    return value
                await asyncio.sleep(POLL)
    except TimeoutError as error:
        raise AssertionError(f"timed out waiting for {what}") from error


@pytest.fixture
async def pair(
    runtime_binary: Path, tmp_path: Path
) -> AsyncIterator[tuple[ControlClient, ControlClient]]:
    """A station with one float point, and a client connected to it.

    The client starts the transfer and interrogates the station, so that its point image
    holds the initial value. Yields the station's and the client's control APIs. Both
    runtimes stop when the test ends, and each must exit cleanly.
    """
    points = tmp_path / "points.json"
    points.write_text(json.dumps(POINTS), encoding="utf-8")
    token_file = write_control_token(tmp_path)
    token = read_control_token(token_file)
    station = LocalRuntime(
        runtime_binary,
        [
            "--listen",
            "127.0.0.1:0",
            "--points",
            str(points),
            "--control",
            "127.0.0.1:0",
            "--control-token-file",
            str(token_file),
        ],
    )
    station_addresses = await station.start()
    assert station_addresses.station is not None, "the station reported no address"
    client = LocalRuntime(
        runtime_binary,
        [
            "--connect",
            station_addresses.station,
            "--control",
            "127.0.0.1:0",
            "--control-token-file",
            str(token_file),
        ],
    )
    client_addresses = await client.start()
    station_api = ControlClient(station_addresses.control, token)
    client_api = ControlClient(client_addresses.control, token)
    try:
        await until(_state(client_api, "connection", "up"), "the client to connect")
        await client_api.start_transfer()
        await until(_state(client_api, "transfer", "started"), "the transfer to start")
        await client_api.general_interrogation()
        await until(_point_held(client_api), "the initial value")
        yield station_api, client_api
    finally:
        await station_api.aclose()
        await client_api.aclose()
        client_stop = await client.stop()
        station_stop = await station.stop()
        assert client_stop.exit_code == 0 and not client_stop.killed, client_stop
        assert station_stop.exit_code == 0 and not station_stop.killed, station_stop


def _state(
    client_api: ControlClient, field: str, value: str
) -> Callable[[], Awaitable[bool | None]]:
    async def check() -> bool | None:
        body = await client_api.ready()
        return True if body.get(field) == value else None

    return check


def _point_held(client_api: ControlClient) -> Callable[[], Awaitable[bool | None]]:
    async def check() -> bool | None:
        body = await client_api.image_points()
        return True if any(p["address"] == ADDRESS for p in body["points"]) else None

    return check


def scenario(*steps: dict[str, Any]) -> Scenario:
    return Scenario.model_validate(
        {
            "schema_version": 1,
            "name": "assertions-on-a-station",
            "bench": "basic-pair",
            "steps": list(steps),
        }
    )


def assert_point(value: float, **changes: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "action": "assert_point",
        "client": CLIENT,
        "address": ADDRESS,
        "kind": "float",
        "value": value,
        "tolerance": 0.001,
    }
    step.update(changes)
    return step


def expect_point(value: float, **changes: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "action": "expect_point",
        "client": CLIENT,
        "address": ADDRESS,
        "kind": "float",
        "value": value,
        "tolerance": 0.001,
        "timeout": "5s",
    }
    step.update(changes)
    return step


async def update_later(station_api: ControlClient, value: float) -> None:
    """The station updates the point after the delay, as a spontaneous change."""
    await asyncio.sleep(UPDATE_DELAY)
    await station_api.update_point(ADDRESS, value)


async def run_with_update(
    client_api: ControlClient,
    station_api: ControlClient,
    steps: Scenario,
    value: float,
) -> Result:
    """Runs the scenario while the station updates the point to `value`."""
    publisher = asyncio.create_task(update_later(station_api, value))
    try:
        return await execute(
            steps, ASSERTION_HANDLERS, ControlSource({CLIENT: client_api})
        )
    finally:
        await publisher


async def test_a_scenario_of_assertions_passes_against_a_station(
    pair: tuple[ControlClient, ControlClient],
) -> None:
    station_api, client_api = pair
    passing = scenario(
        assert_point(
            INITIAL,
            cause=CAUSE_INTERROGATED,
            common_address=1,
            stamped=False,
        ),
        expect_point(
            UPDATED,
            cause=CAUSE_SPONTANEOUS,
            common_address=1,
            stamped=False,
        ),
        assert_point(UPDATED, cause=CAUSE_SPONTANEOUS),
    )
    result = await run_with_update(client_api, station_api, passing, UPDATED)
    assert result.status is RunStatus.PASSED, result.verdicts
    assert [v.outcome for v in result.verdicts] == [Outcome.PASSED] * 3
    assert result.verdicts[1].detail.startswith(f"point {ADDRESS} observed at seq ")


async def test_a_wrong_value_times_out_and_says_what_arrived(
    pair: tuple[ControlClient, ControlClient],
) -> None:
    station_api, client_api = pair
    timing_out = scenario(expect_point(99.0, timeout="2s"))
    result = await run_with_update(client_api, station_api, timing_out, REJECTED)
    assert result.status is RunStatus.FAILED
    assert [v.outcome for v in result.verdicts] == [Outcome.TIMED_OUT]
    assert f"the value is {REJECTED}, not 99.0 (tolerance 0.001)" in (
        result.verdicts[0].detail
    )


async def test_an_assertion_on_the_image_reports_the_wrong_value_and_cause(
    pair: tuple[ControlClient, ControlClient],
) -> None:
    _, client_api = pair
    source = ControlSource({CLIENT: client_api})
    # The image holds the initial value, interrogated with cause 20.
    wrong_value = scenario(assert_point(99.0))
    result = await execute(wrong_value, ASSERTION_HANDLERS, source)
    assert result.status is RunStatus.FAILED
    assert result.verdicts[0].detail == (
        f"the value is {INITIAL}, not 99.0 (tolerance 0.001)"
    )

    wrong_cause = scenario(assert_point(INITIAL, cause=CAUSE_SPONTANEOUS))
    result = await execute(wrong_cause, ASSERTION_HANDLERS, source)
    assert result.status is RunStatus.FAILED
    assert (
        result.verdicts[0].detail
        == f"the cause is {CAUSE_INTERROGATED}, not {CAUSE_SPONTANEOUS}"
    )
