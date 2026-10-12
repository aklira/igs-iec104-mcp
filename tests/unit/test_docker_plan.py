"""The plan of a bench, before anything is created (IMPLEMENTATION.md §5, §6; task
D2): what the runtime can run is planned, and what it cannot is refused by name, all at
once."""

import copy
import json
from typing import Any

import pytest
from bench_data import BENCH
from pydantic import ValidationError

from igs_iec104_mcp.docker import plan
from igs_iec104_mcp.docker.plan import (
    POINTS_FILE,
    PlanError,
    client_arguments,
    plan_bench,
    relay_arguments,
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
    assert plan.server_of == {"client-1": ("server-1",)}
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
    arguments = client_arguments(client, ["172.18.0.2:2404"])
    assert arguments[arguments.index("--connect") + 1] == "172.18.0.2:2404"
    assert "--points" not in arguments


def test_the_event_buffer_is_the_capacity_of_the_runtime() -> None:
    data = copy.deepcopy(BENCH)
    instance(data, "server-1")["limits"] = {"event_buffer": 500}
    station = plan_bench(Bench.model_validate(data)).servers[0]
    arguments = server_arguments(station)
    assert arguments[arguments.index("--event-capacity") + 1] == "500"


def test_a_point_kind_the_runtime_does_not_serve_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every kind is served now (S4d), so the refusal is tested with a runtime
    # that serves fewer.
    monkeypatch.setattr(plan, "SERVED_KINDS", frozenset({"single", "double"}))
    data = copy.deepcopy(BENCH)
    instance(data, "server-1")["points"] = [
        {"address": 1, "kind": "counter", "value": 7},
        {"address": 2, "kind": "scaled", "value": 3},
    ]
    (issue,) = refused(data)
    assert "server-1" in issue
    assert "counter, scaled" in issue
    assert "S4" in issue


def test_every_kind_of_the_model_is_served_by_the_runtime() -> None:
    from igs_iec104_mcp.models.points import PointKind

    assert {str(kind) for kind in PointKind} == plan.SERVED_KINDS


def test_link_parameters_are_passed_to_the_runtime_as_flags() -> None:
    data = copy.deepcopy(BENCH)
    instance(data, "client-1")["parameters"] = {"t1": 20, "t2": 5, "k": 4}
    plan_ = plan_bench(Bench.model_validate(data))
    arguments = client_arguments(plan_.clients[0], ["172.18.0.2:2404"])
    assert arguments[arguments.index("--t1") + 1] == "20"
    assert arguments[arguments.index("--t2") + 1] == "5"
    assert arguments[arguments.index("--k") + 1] == "4"
    assert arguments[arguments.index("--t0") + 1] == "30"
    assert arguments[arguments.index("--w") + 1] == "8"


def test_a_station_gets_its_link_flags_too() -> None:
    station = plan_bench(Bench.model_validate(BENCH)).servers[0]
    arguments = server_arguments(station)
    assert arguments[arguments.index("--t3") + 1] == "20"


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


def test_a_client_with_two_connections_is_planned_as_a_redundancy_group() -> None:
    data = copy.deepcopy(BENCH)
    data["instances"].append(
        {"name": "server-2", "role": "server", "network": "lan", "common_address": 2}
    )
    data["connections"].append({"client": "client-1", "server": "server-2"})
    plan_ = plan_bench(Bench.model_validate(data))
    assert plan_.server_of == {"client-1": ("server-1", "server-2")}
    arguments = client_arguments(plan_.clients[0], ["10.0.0.1:2404", "10.0.0.2:2404"])
    connects = [arguments[i + 1] for i, a in enumerate(arguments) if a == "--connect"]
    assert connects == ["10.0.0.1:2404", "10.0.0.2:2404"]


def test_a_redundancy_group_holds_at_most_eight_stations() -> None:
    data = copy.deepcopy(BENCH)
    data["connections"] = []
    for number in range(2, 11):
        data["instances"].append(
            {
                "name": f"server-{number}",
                "role": "server",
                "network": "lan",
                "common_address": number,
            }
        )
        data["connections"].append({"client": "client-1", "server": f"server-{number}"})
    data["connections"].append({"client": "client-1", "server": "server-1"})
    (issue,) = refused(data)
    assert "a redundancy group holds at most 8" in issue


def test_every_problem_is_reported_together() -> None:
    data = copy.deepcopy(BENCH)
    data["connections"] = []
    instance(data, "server-1")["limits"] = {"pids": 8}
    assert len(refused(data)) == 2


def test_the_runtime_limits_that_it_cannot_take_are_refused() -> None:
    data = copy.deepcopy(BENCH)
    instance(data, "client-1")["limits"] = {"pids": 8}
    (issue,) = refused(data)
    assert "client-1" in issue
    assert "pids" in issue


def test_a_relay_listens_on_the_control_port_and_forwards_to_the_runtime() -> None:
    assert relay_arguments("172.20.0.5:8080") == (
        "--listen",
        "0.0.0.0:8080",
        "--target",
        "172.20.0.5:8080",
    )


def test_the_name_of_the_control_network_is_reserved() -> None:
    data = copy.deepcopy(BENCH)
    data["networks"].append({"name": "control"})
    (issue,) = refused(data)
    assert "reserves" in issue
