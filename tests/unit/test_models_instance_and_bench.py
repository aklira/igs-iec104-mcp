"""Instances, external targets, and the bench as a whole: the rules of §5 and §6."""

import copy
from typing import Any

import pytest
from bench_data import BENCH
from pydantic import ValidationError

from igs_iec104_mcp.models.bench import TestBench as Bench
from igs_iec104_mcp.models.instance import Instance, LinkParameters


def bench(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(BENCH)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def refused(data: dict[str, Any], pattern: str) -> None:
    with pytest.raises(ValidationError, match=pattern):
        Bench.model_validate(data)


def test_the_basic_bench_is_valid() -> None:
    assert Bench.model_validate(BENCH).name == "basic-pair"


def test_a_bench_without_schema_version_or_with_another_is_refused() -> None:
    refused(bench(schema_version=None), "schema_version")
    refused(bench(schema_version=2), "schema_version")


def test_a_name_must_be_lower_case_and_short() -> None:
    refused(bench(name="Basic Pair"), "is not a valid name")


@pytest.mark.parametrize(
    ("parameter", "value", "message"),
    [
        ("t0", 0, "greater than or equal to 1"),
        ("t1", 256, "less than or equal to 255"),
        ("t2", 15, "t2 .* must be below t1"),
        ("t3", 0, "greater than or equal to 1"),
        ("k", 0, "greater than or equal to 1"),
        ("w", 32_768, "less than or equal to 32767"),
        ("t0", 1.5, "valid integer"),
    ],
)
def test_a_link_parameter_outside_the_upstream_rules_is_refused(
    parameter: str, value: object, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        LinkParameters.model_validate({parameter: value})


def test_the_link_parameters_default_to_the_values_of_the_standard() -> None:
    parameters = LinkParameters()
    assert (parameters.t0, parameters.t1, parameters.t2, parameters.t3) == (
        30,
        15,
        10,
        20,
    )
    assert (parameters.k, parameters.w) == (12, 8)


def test_a_client_serves_no_points() -> None:
    client = {
        "name": "client-1",
        "role": "client",
        "network": "lan",
        "common_address": 1,
        "points": [{"address": 1, "kind": "single", "value": True}],
    }
    with pytest.raises(ValidationError, match="is a client, which serves no points"):
        Instance.model_validate(client)


def test_two_points_at_one_address_are_refused() -> None:
    server = copy.deepcopy(BENCH["instances"][1])
    server["points"].append({"address": 100, "kind": "single", "value": True})
    with pytest.raises(ValidationError, match="two points at address 100"):
        Instance.model_validate(server)


def test_a_common_address_outside_the_station_range_is_refused() -> None:
    data = copy.deepcopy(BENCH["instances"][0])
    data["common_address"] = 65535
    with pytest.raises(ValidationError, match="common address 65535 is out of range"):
        Instance.model_validate(data)


def test_resource_limits_are_refused_when_out_of_range() -> None:
    data = copy.deepcopy(BENCH["instances"][0])
    data["limits"] = {"memory_mb": 8}
    with pytest.raises(ValidationError, match="memory_mb"):
        Instance.model_validate(data)


def test_an_instance_on_an_undeclared_network_is_refused() -> None:
    data = bench()
    data["instances"][0]["network"] = "dmz"
    refused(data, "is on network 'dmz', which the bench does not declare")


def test_two_networks_with_one_name_are_refused() -> None:
    refused(
        bench(networks=[{"name": "lan"}, {"name": "lan"}]),
        "two networks have the same name",
    )


def test_an_instance_and_a_target_cannot_share_a_name() -> None:
    target = {
        "name": "client-1",
        "role": "server",
        "network": "wan",
        "endpoint": {"host": "10.0.0.2", "port": 2404},
        "common_address": 1,
    }
    refused(bench(external_targets=[target]), "is used twice")


def test_a_connection_to_an_unknown_name_is_refused() -> None:
    refused(
        bench(connections=[{"client": "client-1", "server": "server-9"}]),
        "'server-9' is neither an instance nor an external target",
    )


def test_a_connection_needs_a_client_and_a_server() -> None:
    refused(
        bench(connections=[{"client": "server-1", "server": "client-1"}]),
        "is a server, so it cannot start a connection",
    )


def test_a_connection_between_instances_needs_one_network() -> None:
    data = bench()
    data["networks"].append({"name": "other"})
    data["instances"][1]["network"] = "other"
    refused(data, "put both on the same network")


def test_a_connection_declared_twice_is_refused() -> None:
    twice = [{"client": "client-1", "server": "server-1"}] * 2
    refused(bench(connections=twice), "is declared twice")


def test_an_external_target_needs_an_external_network() -> None:
    data = bench()
    data["networks"] = [{"name": "lan"}]
    data["instances"][0]["network"] = "lan"
    data["external_targets"] = [
        {
            "name": "plant",
            "role": "server",
            "network": "lan",
            "endpoint": {"host": "10.0.0.2", "port": 2404},
            "common_address": 1,
        }
    ]
    refused(data, "which is not external")


def test_a_target_key_written_inline_is_refused() -> None:
    target = {
        "name": "plant",
        "role": "server",
        "network": "wan",
        "endpoint": {"host": "10.0.0.2", "port": 2404},
        "common_address": 1,
        "tls": {
            "certificate": "secret:plant-cert",
            "key": "-----BEGIN PRIVATE KEY-----",
        },
    }
    refused(bench(external_targets=[target]), "is not a secret reference")


def test_an_endpoint_port_outside_the_range_is_refused() -> None:
    target = {
        "name": "plant",
        "role": "server",
        "network": "wan",
        "endpoint": {"host": "10.0.0.2", "port": 0},
        "common_address": 1,
    }
    refused(bench(external_targets=[target]), "greater than or equal to 1")


def test_an_operation_listed_twice_is_refused() -> None:
    target = {
        "name": "plant",
        "role": "server",
        "network": "wan",
        "endpoint": {"host": "10.0.0.2", "port": 2404},
        "common_address": 1,
        "allowed_operations": ["interrogate", "interrogate"],
    }
    refused(bench(external_targets=[target]), "is listed twice")


def test_an_unknown_operation_is_refused() -> None:
    target = {
        "name": "plant",
        "role": "server",
        "network": "wan",
        "endpoint": {"host": "10.0.0.2", "port": 2404},
        "common_address": 1,
        "allowed_operations": ["reboot"],
    }
    refused(bench(external_targets=[target]), "reboot")
