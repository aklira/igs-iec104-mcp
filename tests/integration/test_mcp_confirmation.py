"""A command to an external target, confirmed by a person through MCP (IMPLEMENTATION.md
§9; task M3).

A real MCP client runs a scenario that sends a command to an external target. A client
that supports elicitation is asked, and its answer is journaled with how it was given. A
client that cannot be asked gets no confirmation, and the command is refused. Each run
removes its bench, as any run does. The images are named as for the run tests.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ElicitResult
from test_mcp_run import BENCH, SOURCE, call, images, until_ended

from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.models.loader import load_document
from igs_iec104_mcp.scenarios.runner import PROJECT_LABEL

EXTERNAL = {
    "name": "ext",
    "role": "client",
    "network": "wan",
    "endpoint": {"host": "127.0.0.1", "port": 2404},
    "common_address": 1,
    "allowed_operations": ["command"],
}
SCENARIO = """\
schema_version: 1
name: command-to-external
bench: basic-pair
steps:
  - action: send_command
    target: ext
    address: 100
    command: single
    value: true
    timeout: 60s
"""


def bench_with_external_target() -> str:
    """The basic pair, with the external target on an external network named wan."""
    data = load_document(BENCH)
    data["networks"].append({"name": "wan", "external": True})
    data["external_targets"] = [EXTERNAL]
    return json.dumps(data)


async def accept(context: Any, params: Any) -> ElicitResult:
    """A person who accepts the question."""
    return ElicitResult(action="accept", content={"confirm": True})


def parameters(project: Path) -> StdioServerParameters:
    runtime, relay = images()
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "igs_iec104_mcp",
            "--project",
            str(project),
            "--runtime-image",
            runtime,
            "--relay-image",
            relay,
        ],
        env={**os.environ, "PYTHONPATH": str(SOURCE)},
    )


async def run_command(project: Path, callback: Any) -> tuple[dict[str, Any], str]:
    """Runs the scenario through a client with `callback` (None: no elicitation), and
    returns the final record of the run and its identity."""
    async with (
        stdio_client(parameters(project)) as (read, write),
        ClientSession(read, write, elicitation_callback=callback) as session,
    ):
        await session.initialize()
        await call(
            session,
            "bench_create",
            name="basic-pair",
            text=bench_with_external_target(),
        )
        await call(
            session, "scenario_create", name="command-to-external", text=SCENARIO
        )
        started = await call(session, "scenario_run", name="command-to-external")
        record = await until_ended(session, started["id"])
        events = await call(session, "run_events", run_id=started["id"], limit=50)
    return {**record, "events": events["events"]}, started["id"]


def only(events: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    found = [event for event in events if event["kind"] == kind]
    assert len(found) == 1, found
    return found[0]


async def test_a_person_confirms_the_command_through_the_client(tmp_path: Path) -> None:
    record, run_id = await run_command(tmp_path, accept)
    confirmed = only(record["events"], "confirmation")
    assert confirmed["accepted"] is True
    assert confirmed["how"] == "elicitation"
    assert only(record["events"], "verdict")["outcome"] == "unsupported"
    assert record["status"] == "error"
    owner = Ownership(project=PROJECT_LABEL, bench="basic-pair", run=run_id)
    assert await DockerAdapter.connect().owned(owner) == []


async def test_a_client_that_cannot_be_asked_gets_no_confirmation(
    tmp_path: Path,
) -> None:
    record, run_id = await run_command(tmp_path, None)
    refused = only(record["events"], "confirmation")
    assert refused["accepted"] is False
    assert refused["how"] == "none"
    assert only(record["events"], "verdict")["outcome"] == "failed"
    assert record["status"] == "failed"
    owner = Ownership(project=PROJECT_LABEL, bench="basic-pair", run=run_id)
    assert await DockerAdapter.connect().owned(owner) == []
