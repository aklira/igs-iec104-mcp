"""A connection through a fault proxy on Docker (IMPLEMENTATION.md §6, §8; task V3b).

The basic pair's connection goes through a fault proxy on the bench network. The client
connects to the proxy, and the proxy forwards to the station. With no fault injected,
the client starts its transfer and interrogates the station through the proxy, and the
proxy reports mode `none` with the connection it carries. The bench is removed
afterwards, and the proxy's logs are kept as evidence. The images are named as for the
other Docker tests; the relay image carries the proxy program.
"""

import json
import secrets as random
from pathlib import Path

from test_docker_group import identity, settled

from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.bench import start_bench
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.models.loader import load_bench, load_document

ROOT = Path(__file__).resolve().parents[2]
BASIC_PAIR = ROOT / "examples/benches/basic-pair.yaml"
PROJECT = "igs-test"


def proxied_bench() -> str:
    """The basic pair, with its one connection through a fault proxy."""
    data = load_document(BASIC_PAIR.read_text(encoding="utf-8"))
    data["name"] = "proxied-pair"
    data["connections"][0]["fault_proxy"] = True
    return json.dumps(data, default=str)


async def test_a_client_reaches_its_station_through_the_fault_proxy(
    adapter: DockerAdapter, tmp_path: Path
) -> None:
    runtime = identity("IGS_RUNTIME_IMAGE", "docker/runtime.Dockerfile")
    relay = identity("IGS_RELAY_IMAGE", "docker/relay.Dockerfile")
    bench = load_bench(proxied_bench())
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
        await settled(client, lambda body: body.get("connection") == "up")
        await client.start_transfer()
        ready = await settled(client, lambda body: body.get("transfer") == "started")
        assert ready["connection"] == "up"
        interrogated = await client.general_interrogation()
        assert interrogated["accepted"] is True
        fault = running.fault("client-1", "server-1")
        try:
            state = await fault.state()
        finally:
            await fault.aclose()
        assert state["mode"] == "none"
        assert state["open"] >= 1, "the client's connection is carried by the proxy"
    finally:
        await client.aclose()
        report = await running.stop()
    assert report.graceful, report
    assert (tmp_path / "evidence" / "proxy-1.log").exists()
    owner = Ownership(project=PROJECT, bench=bench.name, run=run)
    assert await adapter.owned(owner) == [], "the bench was removed"
