"""One station serves the data transfer of one client at a time (issue #4).

A connection that starts its transfer closes the other connections of its station that
are not stopped. A step that needs a superseded client's data cannot pass, so the
scenario check refuses it, in the order of the steps. A parallel step and a redundancy
group are not judged.
"""

import copy
from typing import Any

import pytest
from bench_data import BENCH

from igs_iec104_mcp.models.bench import TestBench as Bench
from igs_iec104_mcp.models.common import DefinitionError
from igs_iec104_mcp.models.scenario import Scenario, check_scenario


def bench(*, second_station: bool = False, group: bool = False) -> Bench:
    """Two clients on one station, or on two stations, or a client of a group."""
    data = copy.deepcopy(BENCH)
    data["instances"].append(
        {"name": "client-2", "role": "client", "network": "lan", "common_address": 1}
    )
    data["connections"].append({"client": "client-2", "server": "server-1"})
    if second_station:
        station = next(i for i in data["instances"] if i["name"] == "server-1")
        data["instances"].append({**station, "name": "server-2", "common_address": 2})
        data["connections"] = [
            {"client": "client-1", "server": "server-1"},
            {"client": "client-2", "server": "server-2"},
        ]
    if group:
        data["connections"].append({"client": "client-1", "server": "server-2"})
        station = next(i for i in data["instances"] if i["name"] == "server-1")
        data["instances"].append({**station, "name": "server-2", "common_address": 2})
    return Bench.model_validate(data)


def start(client: str) -> dict[str, Any]:
    return {"action": "start_transfer", "client": client}


def interrogate(client: str) -> dict[str, Any]:
    return {"action": "general_interrogation", "client": client}


def expect(client: str) -> dict[str, Any]:
    return {
        "action": "expect_point",
        "client": client,
        "address": 100,
        "kind": "float",
        "value": 12.5,
        "timeout": "2s",
    }


def scenario(steps: list[dict[str, Any]]) -> Scenario:
    return Scenario.model_validate(
        {"schema_version": 1, "name": "order", "bench": "basic-pair", "steps": steps}
    )


def problems(steps: list[dict[str, Any]], target: Bench) -> list[str]:
    with pytest.raises(DefinitionError) as error:
        check_scenario(scenario(steps), target)
    return [str(issue) for issue in error.value.issues]


def test_an_interrogation_before_the_second_client_starts_is_valid() -> None:
    check_scenario(
        scenario([start("client-1"), interrogate("client-1"), start("client-2")]),
        bench(),
    )


def test_an_interrogation_after_the_second_client_started_is_refused() -> None:
    found = problems(
        [start("client-1"), start("client-2"), interrogate("client-1")], bench()
    )
    assert len(found) == 1
    assert found[0].startswith("steps[2]: 'client-1' was superseded by 'client-2'")


def test_the_client_that_started_last_is_not_superseded() -> None:
    check_scenario(
        scenario([start("client-1"), start("client-2"), interrogate("client-2")]),
        bench(),
    )


def test_an_expected_point_of_a_superseded_client_is_refused() -> None:
    found = problems(
        [start("client-1"), start("client-2"), expect("client-1")], bench()
    )
    assert any("steps[2]" in issue and "superseded" in issue for issue in found)


def test_starting_the_first_client_again_supersedes_the_second() -> None:
    check_scenario(
        scenario(
            [
                start("client-1"),
                start("client-2"),
                start("client-1"),
                interrogate("client-1"),
            ]
        ),
        bench(),
    )


def test_a_stopped_second_client_does_not_restore_the_first() -> None:
    found = problems(
        [
            start("client-1"),
            start("client-2"),
            {"action": "stop_instance", "instance": "client-2"},
            interrogate("client-1"),
        ],
        bench(),
    )
    assert any("steps[3]" in issue for issue in found)


def test_clients_on_two_stations_do_not_supersede_each_other() -> None:
    check_scenario(
        scenario([start("client-1"), start("client-2"), interrogate("client-1")]),
        bench(second_station=True),
    )


def test_a_client_of_a_redundancy_group_is_not_judged() -> None:
    check_scenario(
        scenario([start("client-1"), start("client-2"), interrogate("client-1")]),
        bench(group=True),
    )


def test_a_parallel_step_is_not_judged() -> None:
    steps: list[dict[str, Any]] = [
        {"action": "parallel", "branches": [[start("client-1")], [start("client-2")]]},
        interrogate("client-1"),
    ]
    check_scenario(scenario(steps), bench())


def test_an_assertion_of_the_image_is_not_refused() -> None:
    assertion = {
        "action": "assert_point",
        "client": "client-1",
        "address": 100,
        "kind": "float",
        "value": 12.5,
    }
    check_scenario(scenario([start("client-1"), start("client-2"), assertion]), bench())
