"""An independent client against a managed station (IMPLEMENTATION.md §13; X3b).

The managed station is a bench of its own: one station of this project, with one point
(IOA 100, short float 12.5), on a Docker engine. The independent peer's client (the
OpenMUC j60870 library, run as a black box in its container, docker/independent) joins
the bench's network, connects to the station, starts the data transfer, sends a station
interrogation, and prints the point it receives. The peer's exit status and its output
are checked here, and so is the station's own record of the connection.

The peer reaches the station on the bench network, not through a published port: the
publication of a station to the host is the X2 path, and it stays covered by its own
test. The images are named by IGS_RUNTIME_IMAGE, IGS_RELAY_IMAGE, and
IGS_INDEPENDENT_IMAGE.
"""

import asyncio
import secrets as random
from pathlib import Path

from test_docker_group import identity

from docker import from_env  # type: ignore[attr-defined]
from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.bench import RunningBench, start_bench
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.docker.specs import resource_name
from igs_iec104_mcp.models.loader import load_bench

PROJECT = "igs-test"
STATION_BENCH = """\
schema_version: 1
name: x3-station
networks:
  - name: lan
instances:
  - name: server-1
    role: server
    network: lan
    common_address: 1
    points:
      - address: 100
        kind: float
        value: 12.5
"""
PEER_ENTRYPOINT = ["java", "-cp", "/opt/peer/classes:/opt/peer/j60870.jar", "Client"]
PEER_PATIENCE_SECONDS = 90


async def station_address(adapter: DockerAdapter, running: RunningBench) -> str:
    """The station's address on the bench network, and the port it bound."""
    member = running.members["server-1"]
    port = str(member.listen).rsplit(":", 1)[1]
    attrs = await adapter.inspect(member.resource)
    networks = attrs.get("NetworkSettings", {}).get("Networks") or {}
    entry = networks[resource_name(running.ownership, "lan")]
    return f"{entry['IPAddress']}:{port}"


async def test_an_independent_client_interrogates_a_managed_station(
    adapter: DockerAdapter, tmp_path: Path
) -> None:
    runtime = identity("IGS_RUNTIME_IMAGE", "docker/runtime.Dockerfile")
    relay = identity("IGS_RELAY_IMAGE", "docker/relay.Dockerfile")
    peer = identity("IGS_INDEPENDENT_IMAGE", "docker/independent/Dockerfile")
    bench = load_bench(STATION_BENCH)
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
    ownership = Ownership(project=PROJECT, bench=bench.name, run=run)
    try:
        address = await station_address(adapter, running)
        host, port = address.rsplit(":", 1)
        docker = from_env()
        container = docker.containers.create(
            peer,
            entrypoint=PEER_ENTRYPOINT,
            command=[
                "--host",
                host,
                "--port",
                port,
                "--common-address",
                "1",
            ],
            network=resource_name(ownership, "lan"),
        )
        try:
            container.start()
            result = await asyncio.to_thread(
                container.wait, timeout=PEER_PATIENCE_SECONDS
            )
            output = container.logs().decode("utf-8", "replace")
        finally:
            container.remove(force=True)
        assert result["StatusCode"] == 0, output
        assert "point 100 value 12.5" in output, output
        assert "interrogation ended" in output, output

        station = running.client("server-1")
        try:
            events = await station.station_events()
        finally:
            await station.aclose()
        assert "connected" in [event["kind"] for event in events["events"]]
    finally:
        report = await running.stop()
    assert report.graceful, report
    assert await adapter.owned(ownership) == [], "the bench was removed"
