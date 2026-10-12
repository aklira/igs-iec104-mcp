"""A client of a redundancy group on Docker (IMPLEMENTATION.md §6, §10; task V2b).

The basic pair gets a second station with the same point, and the client connects to
both. The bench starts: the client sees two stations, and its group starts the data
transfer of one of them without any request. The images are named as for the other
Docker tests.
"""

import asyncio
import json
import os
import secrets as random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from docker.errors import NotFound

from docker import from_env  # type: ignore[attr-defined]
from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.bench import start_bench
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.models.loader import load_bench, load_document
from igs_iec104_mcp.runtime.api import ControlClient

ROOT = Path(__file__).resolve().parents[2]
BASIC_PAIR = ROOT / "examples/benches/basic-pair.yaml"
PROJECT = "igs-test"
PATIENCE_STEPS = 300
POLL_SECONDS = 0.1


def group_bench() -> str:
    """The basic pair with a second station, `server-2`, serving the same points, and
    the client connected to both."""
    data = load_document(BASIC_PAIR.read_text(encoding="utf-8"))
    station = next(i for i in data["instances"] if i["name"] == "server-1")
    second = {**station, "name": "server-2", "common_address": 2}
    data["instances"].append(second)
    data["connections"].append({"client": "client-1", "server": "server-2"})
    return json.dumps(data, default=str)


def identity(variable: str, dockerfile: str) -> str:
    reference = os.environ.get(variable)
    message = f"set {variable} to the image built from {dockerfile}"
    if not reference:
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    try:
        value: str = from_env().images.get(reference).id
    except NotFound:
        pytest.fail(f"no image {reference!r} on the engine: {message}")
    return value


async def settled(
    client: ControlClient, predicate: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    """The client's readiness once it reaches the state `predicate` names."""
    body: dict[str, Any] = {}
    for _ in range(PATIENCE_STEPS):
        body = await client.ready()
        if predicate(body):
            return body
        await asyncio.sleep(POLL_SECONDS)
    raise AssertionError(f"the client did not reach the state: {body}")


async def test_a_client_of_two_stations_starts_one_of_them(
    adapter: DockerAdapter, tmp_path: Path
) -> None:
    runtime = identity("IGS_RUNTIME_IMAGE", "docker/runtime.Dockerfile")
    relay = identity("IGS_RELAY_IMAGE", "docker/relay.Dockerfile")
    bench = load_bench(group_bench())
    run = random.token_hex(16)
    running = await start_bench(
        adapter,
        bench,
        project=PROJECT,
        run=run,
        image=runtime,
        relay=relay,
        evidence=tmp_path / "evidence",
        ready_timeout=60.0,
    )
    try:
        client = running.client("client-1")
        ready = await settled(
            client,
            lambda body: (
                body.get("transfer") == "started"
                and body.get("started_endpoint") is not None
            ),
        )
        assert ready["endpoints"] == 2
        assert ready["connection"] == "up"
        assert ready["started_endpoint"] in (0, 1)
    finally:
        report = await running.stop()
    assert report.graceful, report
    owner = Ownership(project=PROJECT, bench=bench.name, run=run)
    assert await adapter.owned(owner) == [], "the bench was removed"
