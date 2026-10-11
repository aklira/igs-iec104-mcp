"""The external targets (IMPLEMENTATION.md §5, §9; task X1).

The control plane refuses an operation a target does not list. The static report
makes no connection, and a probe is an explicit TCP connection that sends no IEC frame.
"""

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from bench_data import BENCH

from igs_iec104_mcp.definitions import service, targets
from igs_iec104_mcp.definitions.service import ServiceError
from igs_iec104_mcp.definitions.store import ProjectStore
from igs_iec104_mcp.models.common import DefinitionError
from igs_iec104_mcp.models.loader import load_bench
from igs_iec104_mcp.models.scenario import Scenario, check_scenario
from igs_iec104_mcp.models.target import ExternalTarget, Operation, undeclared


def target_data(name: str, port: int, allowed: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "role": "client",
        "network": "wan",
        "endpoint": {"host": "127.0.0.1", "port": port},
        "common_address": 1,
        "allowed_operations": allowed,
    }


def bench_with(*external: dict[str, Any]) -> str:
    bench = json.loads(json.dumps(BENCH))
    bench["external_targets"] = list(external)
    return json.dumps(bench)


def command_scenario(target: str) -> Scenario:
    return Scenario.model_validate(
        {
            "schema_version": 1,
            "name": "command",
            "bench": "basic-pair",
            "steps": [
                {
                    "action": "send_command",
                    "target": target,
                    "address": 100,
                    "command": "single",
                    "value": True,
                }
            ],
        }
    )


@pytest.fixture
async def listener() -> AsyncIterator[tuple[int, list[bytes]]]:
    """A TCP listener that records what each connection sends before it closes."""
    received: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        received.append(await reader.read())
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    yield port, received
    server.close()
    await server.wait_closed()


def test_an_operation_the_target_does_not_list_is_undeclared() -> None:
    target = ExternalTarget.model_validate(target_data("ext", 2404, ["command"]))
    assert undeclared(target, {Operation.COMMAND}) == []
    assert undeclared(target, {Operation.COMMAND, Operation.SETPOINT}) == ["setpoint"]
    silent = ExternalTarget.model_validate(target_data("ext", 2404, []))
    assert undeclared(silent, {Operation.COMMAND}) == ["command"]


def test_a_command_to_a_target_that_does_not_allow_it_is_refused() -> None:
    bench = load_bench(bench_with(target_data("ext-client", 2404, [])))
    with pytest.raises(DefinitionError) as error:
        check_scenario(command_scenario("ext-client"), bench)
    assert "does not allow the 'command' operation (allowed: none)" in str(error.value)


def test_a_command_to_a_target_that_allows_it_passes() -> None:
    bench = load_bench(bench_with(target_data("ext-client", 2404, ["command"])))
    check_scenario(command_scenario("ext-client"), bench)


async def test_the_static_report_connects_to_nothing(
    tmp_path: Path, listener: tuple[int, list[bytes]]
) -> None:
    port, received = listener
    store = ProjectStore(tmp_path)
    service.create(
        store, "bench", "basic-pair", bench_with(target_data("ext", port, ["command"]))
    )
    report = await targets.validate_target(store, "basic-pair", "ext")
    assert report.port == port
    assert report.allowed_operations == ["command"]
    assert report.secured is False
    assert report.probe is None
    await asyncio.sleep(0.1)
    assert received == [], "the report made no connection"


async def test_a_probe_connects_once_and_sends_no_frame(
    tmp_path: Path, listener: tuple[int, list[bytes]]
) -> None:
    port, received = listener
    store = ProjectStore(tmp_path)
    service.create(
        store, "bench", "basic-pair", bench_with(target_data("ext", port, []))
    )
    report = await targets.validate_target(
        store, "basic-pair", "ext", probe=True, timeout=2.0
    )
    assert report.probe is not None
    assert report.probe.reachable is True
    assert "no IEC frame was sent" in report.probe.detail
    await asyncio.sleep(0.1)
    assert received == [b""], "one connection, closed with nothing sent"


async def test_a_probe_of_a_closed_port_says_the_connection_failed(
    tmp_path: Path,
) -> None:
    # Bound but never listening: the port stays reserved for the test, and a connection
    # to it is refused. (A released port could be taken again, and then answer.)
    reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        reserved.bind(("127.0.0.1", 0))
        closed = int(reserved.getsockname()[1])
        store = ProjectStore(tmp_path)
        service.create(
            store, "bench", "basic-pair", bench_with(target_data("ext", closed, []))
        )
        report = await targets.validate_target(
            store, "basic-pair", "ext", probe=True, timeout=2.0
        )
    finally:
        reserved.close()
    assert report.probe is not None
    assert report.probe.reachable is False
    assert report.probe.detail.startswith("the connection failed")


async def test_a_target_that_is_not_declared_is_not_found(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    service.create(store, "bench", "basic-pair", json.dumps(BENCH))
    assert (
        await code_of_async(targets.validate_target(store, "basic-pair", "nothing"))
        == "not_found"
    )
    assert (
        await code_of_async(targets.validate_target(store, "missing-bench", "ext"))
        == "not_found"
    )


async def test_a_probe_timeout_out_of_range_is_invalid_argument(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    service.create(store, "bench", "basic-pair", json.dumps(BENCH))
    for timeout in (0.01, 60.0):
        assert (
            await code_of_async(
                targets.validate_target(
                    store, "basic-pair", "ext", probe=True, timeout=timeout
                )
            )
            == "invalid_argument"
        )


async def code_of_async(request: Any) -> str:
    """The code of the service error a request raises."""
    with pytest.raises(ServiceError) as error:
        await request
    return error.value.code
