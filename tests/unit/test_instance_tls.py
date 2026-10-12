"""The TLS of a bench's runtimes: the definition names secrets, and the plan gives the
runtime its flags (IMPLEMENTATION.md §5, §6; task V1b)."""

import copy
from typing import Any

import pytest
from bench_data import BENCH

from igs_iec104_mcp.docker.plan import (
    TLS_CERTIFICATE_FILE,
    TLS_KEY_FILE,
    TLS_TRUST_FILE,
    client_arguments,
    plan_bench,
    server_arguments,
    tls_file_names,
    tls_references,
)
from igs_iec104_mcp.models.bench import TestBench as Bench

TLS = {
    "certificate": "secret:station-cert",
    "key": "secret:station-key",
    "trust": "secret:root",
}


def secured(
    bench: dict[str, Any],
    station: dict[str, Any] | None = None,
    client: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = copy.deepcopy(bench)
    for instance in data["instances"]:
        if instance["name"] == "server-1" and station is not None:
            instance["tls"] = station
        if instance["name"] == "client-1" and client is not None:
            instance["tls"] = client
    return data


def test_a_station_and_a_client_name_their_secrets() -> None:
    bench = Bench.model_validate(
        secured(BENCH, station=TLS, client={**TLS, "server_name": "station.test"})
    )
    client = next(i for i in bench.instances if i.name == "client-1")
    assert client.tls is not None
    assert client.tls.server_name == "station.test"


def test_a_file_that_is_not_a_secret_reference_is_refused() -> None:
    with pytest.raises(ValueError, match="not a secret reference"):
        Bench.model_validate(secured(BENCH, station={**TLS, "key": "/etc/key.der"}))


def test_a_station_has_no_server_name_to_check() -> None:
    with pytest.raises(ValueError, match="its name is its certificate's"):
        Bench.model_validate(secured(BENCH, station={**TLS, "server_name": "x"}))


def test_the_intermediates_are_limited_to_eight() -> None:
    chain = [f"secret:inter-{i}" for i in range(9)]
    with pytest.raises(ValueError):
        Bench.model_validate(secured(BENCH, station={**TLS, "intermediates": chain}))


def test_the_plan_gives_the_runtime_its_files_and_chain_in_order() -> None:
    chain = ["secret:inter-a", "secret:inter-b"]
    bench = Bench.model_validate(
        secured(BENCH, station={**TLS, "intermediates": chain})
    )
    plan = plan_bench(bench)
    station = next(s for s in plan.servers if s.name == "server-1")
    arguments = server_arguments(station)
    assert (
        arguments[arguments.index("--tls-certificate") + 1]
        == "/run/igs/tls-certificate.der"
    )
    assert arguments[arguments.index("--tls-key") + 1] == "/run/igs/tls-key.der"
    assert arguments[arguments.index("--tls-trust") + 1] == "/run/igs/tls-trust.der"
    intermediates = [
        arguments[i + 1] for i, a in enumerate(arguments) if a == "--tls-intermediate"
    ]
    assert intermediates == [
        "/run/igs/tls-intermediate-0.der",
        "/run/igs/tls-intermediate-1.der",
    ]


def test_a_client_names_its_server_name_in_its_flags() -> None:
    bench = Bench.model_validate(
        secured(BENCH, client={**TLS, "server_name": "station.test"})
    )
    plan = plan_bench(bench)
    client = next(c for c in plan.clients if c.name == "client-1")
    arguments = client_arguments(client, "172.20.0.5:2404")
    assert arguments[arguments.index("--tls-server-name") + 1] == "station.test"


def test_a_plain_runtime_has_no_tls_flags() -> None:
    plan = plan_bench(Bench.model_validate(BENCH))
    station = next(s for s in plan.servers if s.name == "server-1")
    assert all(not a.startswith("--tls") for a in server_arguments(station))


def test_the_material_names_match_the_references_of_the_definition() -> None:
    bench = Bench.model_validate(
        secured(BENCH, station={**TLS, "intermediates": ["secret:inter-a"]})
    )
    station = next(i for i in bench.instances if i.name == "server-1")
    assert station.tls is not None
    references = tls_references(station.tls)
    assert references == {
        TLS_CERTIFICATE_FILE: "secret:station-cert",
        TLS_KEY_FILE: "secret:station-key",
        TLS_TRUST_FILE: "secret:root",
        "tls-intermediate-0.der": "secret:inter-a",
    }
    plan = plan_bench(bench)
    station_plan = next(s for s in plan.servers if s.name == "server-1")
    assert station_plan.tls is not None
    assert tls_file_names(station_plan.tls) == tuple(references)
