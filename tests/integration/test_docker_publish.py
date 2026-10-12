"""A published station is reached from outside Docker (IMPLEMENTATION.md §6; task X2).

The basic pair runs on a Docker engine, with its station published on the host at the
advertised endpoint. A runtime started on the host, outside any container, connects to
that endpoint, starts its transfer, interrogates the station, and reads the value. The
bench is then removed, and no owned resource is left.

The images are named by IGS_RUNTIME_IMAGE and IGS_RELAY_IMAGE, and the runtime binary by
IGS_RUNTIME_BINARY. Without them the test skips, except in CI, where a missing input
fails the run. The host port is chosen by the engine (`host_port: 0`) and reported when
the station starts, so no port is picked before it is bound (D-054).
"""

import asyncio
import json
import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from docker.errors import NotFound

from docker import from_env  # type: ignore[attr-defined]
from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.bench import start_bench
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.models.bench import TestBench as Bench
from igs_iec104_mcp.models.loader import load_bench, load_document
from igs_iec104_mcp.runtime.api import ControlClient
from igs_iec104_mcp.runtime.process import (
    LocalRuntime,
    read_control_token,
    write_control_token,
)

ROOT = Path(__file__).resolve().parents[2]
BASIC_PAIR = ROOT / "examples/benches/basic-pair.yaml"
PROJECT = "igs-test"
# The name the advertised endpoint carries: the loopback, where the test's client runs.
ADVERTISE = "127.0.0.1"
PATIENCE = 60.0
POLL = 0.1
ADDRESS = 100
VALUE = 12.5


@pytest.fixture
def runtime_image() -> str:
    """The identity of the runtime image named by IGS_RUNTIME_IMAGE."""
    return _identity("IGS_RUNTIME_IMAGE", "docker/runtime.Dockerfile")


@pytest.fixture
def relay_image() -> str:
    """The identity of the relay image named by IGS_RELAY_IMAGE."""
    return _identity("IGS_RELAY_IMAGE", "docker/relay.Dockerfile")


def _identity(variable: str, dockerfile: str) -> str:
    reference = os.environ.get(variable)
    message = f"set {variable} to the image built from {dockerfile}"
    if not reference:
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    try:
        identity: str = from_env().images.get(reference).id
    except NotFound:
        pytest.fail(f"no image {reference!r} on the engine: {message}")
    return identity


def published_bench(host_port: int = 0) -> Bench:
    """The basic pair, with its station published on the loopback. A host port of 0
    lets the engine choose the port when the station starts (D-054)."""
    data = load_document(BASIC_PAIR.read_text(encoding="utf-8"))
    for instance in data["instances"]:
        if instance["name"] == "server-1":
            instance["publish"] = {
                "port": 2404,
                "host_port": host_port,
                "advertise": ADVERTISE,
            }
    return load_bench(json.dumps(data))


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


async def test_a_client_outside_docker_reaches_the_published_station(
    adapter: DockerAdapter,
    runtime_image: str,
    relay_image: str,
    runtime_binary: Path,
    tmp_path: Path,
) -> None:
    bench = published_bench()
    run = secrets.token_hex(16)
    running = await start_bench(
        adapter,
        bench,
        project=PROJECT,
        run=run,
        image=runtime_image,
        relay=relay_image,
        evidence=tmp_path / "evidence",
        ready_timeout=60.0,
    )
    try:
        advertised = running.advertised("server-1")
        assert advertised is not None
        host, _, port = advertised.rpartition(":")
        assert host == ADVERTISE
        assert 1 <= int(port) <= 65535, (
            "the engine bound a port, and the bench reports it"
        )

        external_dir = tmp_path / "external"
        external_dir.mkdir()
        token_file = write_control_token(external_dir)
        token = read_control_token(token_file)
        outside = LocalRuntime(
            runtime_binary,
            [
                "--connect",
                advertised,
                "--control",
                "127.0.0.1:0",
                "--control-token-file",
                str(token_file),
            ],
        )
        addresses = await outside.start()
        api = ControlClient(addresses.control, token)
        try:
            await until(_ready(api, "connection", "up"), "the connection")
            await api.start_transfer()
            await until(_ready(api, "transfer", "started"), "the transfer")
            await api.general_interrogation()
            value = await until(_point(api, ADDRESS, VALUE), "the interrogation")
            assert value == VALUE
        finally:
            await api.aclose()
            outside_stop = await outside.stop()
        assert outside_stop.exit_code == 0 and not outside_stop.killed, outside_stop
    finally:
        report = await running.stop()
    assert report.graceful, report
    owner = Ownership(project=PROJECT, bench=bench.name, run=run)
    assert await adapter.owned(owner) == [], "the bench was removed"


def _ready(
    api: ControlClient, field: str, value: str
) -> Callable[[], Awaitable[bool | None]]:
    async def check() -> bool | None:
        body = await api.ready()
        return True if body.get(field) == value else None

    return check


def _point(
    api: ControlClient, address: int, value: float
) -> Callable[[], Awaitable[Any | None]]:
    async def check() -> Any | None:
        body = await api.image_points()
        for point in body["points"]:
            if point["address"] == address and point["value"] == value:
                return point["value"]
        return None

    return check
