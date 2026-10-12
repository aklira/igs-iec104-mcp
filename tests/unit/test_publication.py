"""The published station port (IMPLEMENTATION.md §6; task X2).

The definition names the port, the host address, and the advertised endpoint. Only a
station can be published. The plan imposes the station's port and refuses two stations
on one host port, and the engine is asked for exactly the ports the spec names.
"""

import copy
from typing import Any

import pytest
from bench_data import BENCH

from igs_iec104_mcp.docker.adapter import _ports_of
from igs_iec104_mcp.docker.plan import (
    PlanError,
    plan_bench,
    relay_arguments,
    server_arguments,
)
from igs_iec104_mcp.docker.specs import ContainerSpec, PortPublication, SpecError
from igs_iec104_mcp.models.bench import TestBench as Bench

DIGEST = "sha256:" + "a" * 64


def published(
    bench: dict[str, Any], station: str = "server-1", host_port: int = 2404
) -> dict[str, Any]:
    data = copy.deepcopy(bench)
    for instance in data["instances"]:
        if instance["name"] == station:
            instance["publish"] = {
                "port": 2404,
                "host_port": host_port,
                "advertise": "10.0.0.5",
            }
    return data


def test_a_station_publication_is_read_with_its_defaults() -> None:
    bench = Bench.model_validate(published(BENCH))
    station = next(i for i in bench.instances if i.name == "server-1")
    assert station.publish is not None
    assert station.publish.bind == "127.0.0.1"
    assert station.publish.advertise == "10.0.0.5"


def test_only_a_station_can_be_published() -> None:
    data = copy.deepcopy(BENCH)
    for instance in data["instances"]:
        if instance["name"] == "client-1":
            instance["publish"] = {"port": 2404, "host_port": 2404, "advertise": "h"}
    with pytest.raises(ValueError, match="only a station can be published"):
        Bench.model_validate(data)


@pytest.mark.parametrize(
    "change,message",
    [
        ({"host_port": -1}, "host_port"),
        ({"host_port": 70000}, "host_port"),
        ({"port": 70000}, "port"),
        ({"bind": "not-an-address"}, "not an IP address to publish on"),
        ({"advertise": "10.0.0.5 extra"}, "contains a space"),
    ],
)
def test_a_publication_with_a_bad_value_is_refused(
    change: dict[str, Any], message: str
) -> None:
    data = published(BENCH)
    for instance in data["instances"]:
        if instance["name"] == "server-1":
            instance["publish"].update(change)
    with pytest.raises(ValueError, match=message):
        Bench.model_validate(data)


def test_a_published_station_imposes_its_listening_port() -> None:
    plan = plan_bench(Bench.model_validate(published(BENCH)))
    station = next(s for s in plan.servers if s.name == "server-1")
    assert station.publication is not None
    assert "--listen" in server_arguments(station)
    assert server_arguments(station)[1] == "0.0.0.0:2404"
    other = plan_bench(Bench.model_validate(BENCH)).servers[0]
    assert server_arguments(other)[1] == "0.0.0.0:0"


def test_the_relay_of_a_publication_listens_on_the_station_port() -> None:
    assert relay_arguments("172.20.0.5:2404", port=2404) == (
        "--listen",
        "0.0.0.0:2404",
        "--target",
        "172.20.0.5:2404",
    )


def test_two_stations_cannot_publish_one_host_port() -> None:
    data = published(BENCH, host_port=2404)
    data["instances"].append(
        {
            "name": "server-2",
            "role": "server",
            "network": "lan",
            "common_address": 2,
            "publish": {"port": 2404, "host_port": 2404, "advertise": "10.0.0.5"},
        }
    )
    with pytest.raises(PlanError, match="publishes host port 2404"):
        plan_bench(Bench.model_validate(data))


def test_the_engine_is_asked_for_exactly_the_published_port() -> None:
    spec = ContainerSpec(
        instance="iec-server-1",
        image=DIGEST,
        network="control",
        publish=PortPublication(container_port=2404, bind="127.0.0.1", host_port=2404),
    )
    assert _ports_of(spec) == {"2404/tcp": ("127.0.0.1", 2404)}
    control = ContainerSpec(
        instance="relay-client-1", image=DIGEST, network="control", publish_control=True
    )
    assert _ports_of(control) == {"8080/tcp": ("127.0.0.1", None)}
    plain = ContainerSpec(instance="client-1", image=DIGEST, network="lan")
    assert _ports_of(plain) == {}


def test_a_container_publishes_its_control_port_or_one_port_not_both() -> None:
    with pytest.raises(SpecError, match="not both"):
        ContainerSpec(
            instance="relay-x",
            image=DIGEST,
            network="control",
            publish_control=True,
            publish=PortPublication(
                container_port=2404, bind="127.0.0.1", host_port=2404
            ),
        )


def test_a_publication_names_a_valid_port_and_address() -> None:
    with pytest.raises(SpecError, match="out of range"):
        PortPublication(container_port=0, bind="127.0.0.1", host_port=2404)
    with pytest.raises(SpecError, match="not an IP address"):
        PortPublication(container_port=2404, bind="host", host_port=2404)


def test_two_stations_may_each_let_the_engine_choose_a_host_port() -> None:
    data = published(BENCH, host_port=0)
    data["instances"].append(
        {
            "name": "server-2",
            "role": "server",
            "network": "lan",
            "common_address": 2,
            "publish": {"port": 2404, "host_port": 0, "advertise": "10.0.0.5"},
        }
    )
    result = plan_bench(Bench.model_validate(data))
    assert [p.name for p in result.servers] == ["server-1", "server-2"]


def test_a_host_port_of_zero_is_the_engine_choice_in_the_spec() -> None:
    publication = PortPublication(container_port=2404, bind="127.0.0.1", host_port=0)
    assert publication.host_port == 0
    with pytest.raises(SpecError, match="out of range"):
        PortPublication(container_port=2404, bind="127.0.0.1", host_port=70000)
