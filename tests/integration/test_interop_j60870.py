"""Interoperability with an independent implementation (IMPLEMENTATION.md §13; X3a).

The independent peer is the OpenMUC j60870 library (GNU GPL v3), run as a black box: a
container with a station that answers an interrogation for one point
(docker/independent). Our runtime's client, built on the upstream library igs-iec104,
connects to that station over the network, starts its data transfer, interrogates it,
and holds the station's value. Both ends are IEC 60870-5-104 implementations by
different authors; the test checks the exchange on the wire, not the peer's code.

The peer's port is published on the loopback at a port the engine chooses, and the
runtime connects to it there: no fixed host port is used. The image is named by
IGS_INDEPENDENT_IMAGE (its identity); CI builds it from docker/independent.
"""

import asyncio
import secrets as random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from test_docker_group import identity

from docker import from_env  # type: ignore[attr-defined]
from igs_iec104_mcp.runtime.api import ControlClient
from igs_iec104_mcp.runtime.process import (
    LocalRuntime,
    read_control_token,
    write_control_token,
)

PEER_PORT = 2404
PEER_COMMON_ADDRESS = 1
POINT_ADDRESS = 100
POINT_VALUE = 12.5
PATIENCE_STEPS = 200
POLL_SECONDS = 0.1


@asynccontextmanager
async def peer_started(name: str) -> AsyncIterator[tuple[str, Any]]:
    """The independent station, in its container, with its port on the loopback. Yields
    the host address of the port and the container. The container is removed after."""
    client = from_env()
    image = identity("IGS_INDEPENDENT_IMAGE", "docker/independent/Dockerfile")
    container = client.containers.run(
        image,
        command=[
            "--port",
            str(PEER_PORT),
            "--common-address",
            str(PEER_COMMON_ADDRESS),
        ],
        name=name,
        detach=True,
        ports={f"{PEER_PORT}/tcp": ("127.0.0.1", None)},
    )
    try:
        for _ in range(PATIENCE_STEPS):
            container.reload()
            if b"station listening" in container.logs():
                break
            await asyncio.sleep(POLL_SECONDS)
        else:
            raise AssertionError(f"the peer did not start: {container.logs()!r}")
        port = container.ports[f"{PEER_PORT}/tcp"][0]["HostPort"]
        yield f"127.0.0.1:{port}", container
    finally:
        container.remove(force=True)


async def until(check: Any) -> Any:
    """Calls `check` until it returns a value, within the patience of the test."""
    for _ in range(PATIENCE_STEPS):
        value = await check()
        if value is not None:
            return value
        await asyncio.sleep(POLL_SECONDS)
    raise AssertionError("the state was not reached in time")


async def test_our_client_interrogates_the_independent_station(
    runtime_binary: Path, tmp_path: Path
) -> None:
    name = f"igs-x3-peer-{random.token_hex(4)}"
    async with peer_started(name) as (address, container):
        token = read_control_token(write_control_token(tmp_path))
        runtime = LocalRuntime(
            runtime_binary,
            [
                "--connect",
                address,
                "--common-address",
                str(PEER_COMMON_ADDRESS),
                "--control",
                "127.0.0.1:0",
                "--control-token-file",
                str(tmp_path / "control-token"),
            ],
        )
        addresses = await runtime.start()
        api = ControlClient(addresses.control, token)
        try:
            await until(lambda: _when(api, lambda body: body.get("connection") == "up"))
            await api.start_transfer()
            await until(
                lambda: _when(api, lambda body: body.get("transfer") == "started")
            )
            interrogated = await api.general_interrogation()
            assert interrogated["accepted"] is True

            point = await until(lambda: _point(api))
            assert point["common_address"] == PEER_COMMON_ADDRESS
            assert point["type_id"] == 13, "a short floating point value (M_ME_NC_1)"
            assert point["cause"] == 20, "an answer to the station interrogation"
            assert point["value"] == POINT_VALUE
        finally:
            await api.aclose()
            stopped = await runtime.stop()
        assert stopped.exit_code == 0
        assert b"data transfer started" in container.logs(), (
            "the peer saw the data transfer start"
        )


async def _when(api: ControlClient, predicate: Any) -> dict[str, Any] | None:
    body = await api.ready()
    return body if predicate(body) else None


async def _point(api: ControlClient) -> dict[str, Any] | None:
    body = await api.image_points()
    for point in body["points"]:
        if point["address"] == POINT_ADDRESS:
            found: dict[str, Any] = point
            return found
    return None
