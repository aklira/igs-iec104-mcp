"""A station restarted under a scenario, and the client that reconnects to it.

Task E5 (IMPLEMENTATION.md §8). The station and the client run as local processes, as in
milestone 1, and the instance actions drive them through their control APIs.

The runtime binary is named by IGS_RUNTIME_BINARY (see conftest.py).
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from igs_iec104_mcp.models.run import Outcome, RunStatus
from igs_iec104_mcp.models.scenario import Scenario
from igs_iec104_mcp.runtime.local_bench import LocalBench
from igs_iec104_mcp.runtime.source import ControlSource
from igs_iec104_mcp.scenarios.actions import actions_for
from igs_iec104_mcp.scenarios.assertions import ASSERTION_HANDLERS
from igs_iec104_mcp.scenarios.engine import Result, execute

STATION = "server-1"
CLIENT = "client-1"
ADDRESS = 100
INITIAL = 12.5
UPDATED = 18.0
# The cause of the interrogation answers (IEC 60870-5-104 §8).
CAUSE_INTERROGATED = 20
POINTS: dict[str, Any] = {
    "schema_version": 1,
    "points": [{"address": ADDRESS, "kind": "float", "value": INITIAL}],
}


@pytest.fixture
async def bench(runtime_binary: Path, tmp_path: Path) -> AsyncIterator[LocalBench]:
    """A station and its client, both running. Both stop when the test ends."""
    local = LocalBench(runtime_binary, tmp_path, POINTS, station=STATION, client=CLIENT)
    try:
        await local.start(STATION)
        await local.start(CLIENT)
        yield local
    finally:
        await local.aclose()
        for stop in local.stops:
            assert stop.exit_code == 0 and not stop.killed, stop


async def run(bench: LocalBench, scenario: Scenario) -> Result:
    handlers = {**ASSERTION_HANDLERS, **actions_for(bench)}
    source = ControlSource({CLIENT: bench.control(CLIENT)})
    return await execute(scenario, handlers, source)


def scenario(*steps: dict[str, Any], **changes: Any) -> Scenario:
    data: dict[str, Any] = {
        "schema_version": 1,
        "name": "restart-and-reconnect",
        "bench": "basic-pair",
        "steps": list(steps),
    }
    data.update(changes)
    return Scenario.model_validate(data)


def assert_point(value: float) -> dict[str, Any]:
    return {
        "action": "assert_point",
        "client": CLIENT,
        "address": ADDRESS,
        "kind": "float",
        "value": value,
        "tolerance": 0.001,
        "cause": CAUSE_INTERROGATED,
    }


async def test_a_station_restart_reconnects_the_client_which_interrogates_again(
    bench: LocalBench,
) -> None:
    result = await run(
        bench,
        scenario(
            {"action": "wait_connection", "instance": CLIENT, "timeout": "15s"},
            {"action": "start_transfer", "client": CLIENT},
            {"action": "general_interrogation", "client": CLIENT},
            assert_point(INITIAL),
            {
                "action": "update_point",
                "server": STATION,
                "address": ADDRESS,
                "value": UPDATED,
            },
            {"action": "general_interrogation", "client": CLIENT},
            assert_point(UPDATED),
            {"action": "stop_instance", "instance": STATION, "timeout": "10s"},
            {"action": "start_instance", "instance": STATION, "timeout": "10s"},
            {"action": "wait_connection", "instance": CLIENT, "timeout": "20s"},
            {"action": "start_transfer", "client": CLIENT},
            {"action": "general_interrogation", "client": CLIENT},
            # The restarted station serves its points from the file again: the value is
            # the initial one, and it reached the client through the new interrogation.
            assert_point(INITIAL),
        ),
    )
    assert result.status is RunStatus.PASSED, result.verdicts
    assert [v.outcome for v in result.verdicts] == [Outcome.PASSED] * 13
    assert result.progress.completed == 13
    assert [stop.exit_code for stop in bench.stops] == [0]


async def test_a_reconnect_that_does_not_come_in_time_times_out_and_is_cleaned_up(
    bench: LocalBench,
) -> None:
    result = await run(
        bench,
        scenario(
            {"action": "wait_connection", "instance": CLIENT, "timeout": "15s"},
            {"action": "stop_instance", "instance": STATION, "timeout": "10s"},
            {"action": "wait_connection", "instance": CLIENT, "timeout": "2s"},
            cleanup=[{"action": "start_instance", "instance": STATION}],
        ),
    )
    assert result.status is RunStatus.FAILED
    assert [v.step for v in result.verdicts] == [
        "step 1: wait_connection",
        "step 2: stop_instance",
        "step 3: wait_connection",
        "cleanup 1: start_instance",
    ]
    assert [v.outcome for v in result.verdicts] == [
        Outcome.PASSED,
        Outcome.PASSED,
        Outcome.TIMED_OUT,
        Outcome.PASSED,
    ]
    assert result.verdicts[2].detail == "no result within 2 s"
    assert result.reason == "stopped after step 3: fail-fast"
    # The cleanup started the station again, on its own addresses.
    assert (await bench.control(STATION).ready())["ready"] is True
