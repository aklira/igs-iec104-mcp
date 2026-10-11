"""The milestone 5 criterion (IMPLEMENTATION.md §13): a real MCP client creates a bench
and a scenario, runs the scenario on a Docker bench, follows the run, and finds the
bench removed afterwards.

The runtime and relay images are named by IGS_RUNTIME_IMAGE and IGS_RELAY_IMAGE (see
docker/runtime.Dockerfile and docker/relay.Dockerfile). Without them the test skips,
except in CI, where missing images fail the run.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from docker.errors import NotFound
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

from docker import from_env  # type: ignore[attr-defined]
from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.scenarios.runner import PROJECT_LABEL

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src"
BENCH = (ROOT / "examples/benches/basic-pair.yaml").read_text(encoding="utf-8")
SCENARIO = (
    ROOT / "examples/scenarios/interrogation-and-spontaneous-update.yaml"
).read_text(encoding="utf-8")
SCENARIO_NAME = "interrogation-and-spontaneous-update"
RUN_TIMEOUT = 300.0
POLL = 0.5


def images() -> tuple[str, str]:
    """The identities of the runtime and relay images. A bench names its images by
    digest, so the server is given identities, as an operator gives them (§6)."""
    names = [os.environ.get("IGS_RUNTIME_IMAGE"), os.environ.get("IGS_RELAY_IMAGE")]
    if not all(names):
        message = (
            "set IGS_RUNTIME_IMAGE and IGS_RELAY_IMAGE to the images built from "
            "docker/runtime.Dockerfile and docker/relay.Dockerfile"
        )
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    engine = from_env()
    identities: list[str] = []
    for reference in names:
        assert reference is not None
        try:
            identities.append(str(engine.images.get(reference).id))
        except NotFound:
            pytest.fail(f"no image {reference!r} on the engine: {message}")
    return identities[0], identities[1]


async def call(session: ClientSession, tool: str, **arguments: Any) -> dict[str, Any]:
    """The structured result of a tool. A tool error fails the test with its text."""
    result: CallToolResult = await session.call_tool(tool, arguments)
    if result.is_error:
        block = result.content[0]
        text = block.text if isinstance(block, TextContent) else str(block)
        pytest.fail(f"{tool} failed: {text}")
    assert result.structured_content is not None
    return dict(result.structured_content)


async def until_ended(session: ClientSession, run_id: str) -> dict[str, Any]:
    async with asyncio.timeout(RUN_TIMEOUT):
        while True:
            record = await call(session, "run_get", run_id=run_id)
            if record["status"] in {"passed", "failed", "cancelled", "error"}:
                return record
            await asyncio.sleep(POLL)


async def test_an_mcp_client_runs_a_scenario_on_a_docker_bench(
    tmp_path: Path,
) -> None:
    runtime, relay = images()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "igs_iec104_mcp",
            "--project",
            str(tmp_path),
            "--runtime-image",
            runtime,
            "--relay-image",
            relay,
        ],
        env={**os.environ, "PYTHONPATH": str(SOURCE)},
    )
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        await call(session, "bench_create", name="basic-pair", text=BENCH)
        await call(session, "scenario_create", name=SCENARIO_NAME, text=SCENARIO)

        started = await call(session, "scenario_run", name=SCENARIO_NAME)
        assert started["status"] == "running"
        run_id = started["id"]

        record = await until_ended(session, run_id)
        assert record["status"] == "passed", record

        events = await call(session, "run_events", run_id=run_id, limit=50)
        verdicts = [e for e in events["events"] if e["kind"] == "verdict"]
        assert [v["outcome"] for v in verdicts] == ["passed"] * 6, verdicts
        assert events["events"][-1]["kind"] == "ended"

        listed = await call(session, "run_list")
        assert [run["id"] for run in listed["items"]] == [run_id]

        evidence = tmp_path / "runs" / run_id / "evidence" / "manifest.json"
        assert evidence.is_file()

    owner = Ownership(project=PROJECT_LABEL, bench="basic-pair", run=run_id)
    adapter = DockerAdapter.connect()
    assert await adapter.owned(owner) == [], "the run removed its bench"
