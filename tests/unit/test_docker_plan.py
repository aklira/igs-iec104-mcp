"""The plan of a bench, before anything is created (IMPLEMENTATION.md §5, §6; task
D2): what the runtime can run is planned, and what it cannot is refused by name, all at
once."""

import copy
import json
from typing import Any

import pytest
from bench_data import BENCH
from pydantic import ValidationError

from igs_iec104_mcp.docker.plan import (
    POINTS_FILE,
    PlanError,
    client_arguments,
    plan_bench,
    runtime_path,
    server_arguments,
)
from igs_iec104_mcp.models.bench import TestBench as Bench
from igs_iec104_mcp.models.instance import LinkParameters


def instance(data: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in data["instances"] if item["name"] == name)


def refused(data: dict[str, Any]) -> list[str]:
    with pytest.raises(PlanError) as error:
        plan_bench(Bench.model_validate(data))
    return error.value.issues


def test_the_link_defaults_are_the_runtimes_defaults() -> None:
    # The runtime's Parameters::default (igs-iec104-link): t0 30 s, t1 15 s, t2 10 s,
    # t3 20 s, k 12, w 8, as IEC 60870-5-104 §9.6 gives them.
    defaults = LinkParameters()
    assert (defaults.t0, defaults.t1, defaults.t2, defaults.t3) == (30, 15, 10, 20)
    assert (defaults.k, defaults.w) == (12, 8)


def test_the_basic_pair_is_planned_with_its_station_and_its_client() -> None:
    plan = plan_bench(Bench.model_validate(BENCH))
    assert [network.name for network in plan.networks] == ["lan", "wan"]
    assert [runtime.name for runtime in plan.servers] == ["server-1"]
    assert [runtime.name for runtime in plan.clients] == ["client-1"]
    assert plan.server_of == {"client-1": "server-1"}
    points = json.loads(plan.servers[0].points)
    assert points["schema_version"] == 1
    assert points["points"][0]["address"] == 100
    assert points["points"][0]["kind"] == "float"


def test_a_station_listens_on_a_port_the_runtime_picks_and_publishes_control() -> None:
    station = plan_bench(Bench.model_validate(BENCH)).servers[0]
    arguments = server_arguments(station)
    assert arguments[arguments.index("--listen") + 1] == "0.0.0.0:0"
    assert arguments[arguments.index("--control") + 1] == "0.0.0.0:8080"
    assert arguments[arguments.index("--points") + 1] == runtime_path(POINTS_FILE)
    assert arguments[arguments.index("--common-address") + 1] == "1"
    assert "--connect" not in arguments


def test_a_client_is_given_the_address_of_its_station() -> None:
    client = plan_bench(Bench.model_validate(BENCH)).clients[0]
    arguments = client_arguments(client, "172.18.0.2:2404")
    assert arguments[arguments.index("--connect") + 1] == "172.18.0.2:2404"
    assert "--points" not in arguments


def test_the_event_buffer_is_the_capacity_of_the_runtime() -> None:
    data = copy.deepcopy(BENCH)
    instance(data, "server-1")["limits"] = {"event_buffer": 500}
    station = plan_bench(Bench.model_validate(data)).servers[0]
    arguments = server_arguments(station)
    assert arguments[arguments.index("--event-capacity") + 1] == "500"


def test_a_point_kind_the_runtime_does_not_serve_is_refused_by_name() -> None:
    data = copy.deepcopy(BENCH)
    instance(data, "server-1")["points"] = [
        {"address": 1, "kind": "counter", "value": 7},
        {"address": 2, "kind": "step", "value": {"value": 1, "transient": False}},
    ]
    (issue,) = refused(data)
    assert "server-1" in issue
    assert "counter, step" in issue
    assert "Q-002" in issue


def test_link_parameters_other_than_the_defaults_are_refused_by_name() -> None:
    data = copy.deepcopy(BENCH)
    instance(data, "client-1")["parameters"] = {"t0": 60, "k": 4}
    (issue,) = refused(data)
    assert "client-1" in issue
    assert "t0, k" in issue
    assert "Q-003" in issue


def test_a_connection_across_two_networks_is_refused_by_the_model() -> None:
    # The bench model already checks the two ends of a connection; the plan's own check
    # is a second guard, reached only by a bench the model did not check.
    data = copy.deepcopy(BENCH)
    data["networks"].append({"name": "other"})
    instance(data, "server-1")["network"] = "other"
    with pytest.raises(ValidationError):
        Bench.model_validate(data)


def test_a_connection_to_an_external_target_is_refused_until_x1() -> None:
    data = copy.deepcopy(BENCH)
    data["external_targets"] = [
        {
            "name": "plant",
            "role": "server",
            "network": "wan",
            "endpoint": {"host": "plant.example", "port": 2404},
            "common_address": 1,
        }
    ]
    data["connections"] = [{"client": "client-1", "server": "plant"}]
    instance(data, "client-1")["network"] = "wan"
    issues = refused(data)
    assert any("external target" in issue and "X1" in issue for issue in issues)


def test_a_client_without_a_connection_is_refused() -> None:
    data = copy.deepcopy(BENCH)
    data["connections"] = []
    (issue,) = refused(data)
    assert "client-1" in issue
    assert "no connection" in issue


def test_a_client_with_two_connections_is_refused() -> None:
    data = copy.deepcopy(BENCH)
    data["instances"].append(
        {"name": "server-2", "role": "server", "network": "lan", "common_address": 2}
    )
    data["connections"].append({"client": "client-1", "server": "server-2"})
    (issue,) = refused(data)
    assert "two connections" in issue


def test_every_problem_is_reported_together() -> None:
    data = copy.deepcopy(BENCH)
    instance(data, "client-1")["parameters"] = {"w": 4}
    instance(data, "server-1")["points"] = [
        {"address": 1, "kind": "packed", "value": {"status": 1, "change": 0}}
    ]
    assert len(refused(data)) == 2


def test_the_runtime_limits_that_it_cannot_take_are_refused() -> None:
    data = copy.deepcopy(BENCH)
    instance(data, "client-1")["limits"] = {"pids": 8}
    (issue,) = refused(data)
    assert "client-1" in issue
    assert "pids" in issue
