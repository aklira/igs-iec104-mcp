"""The reports of a run on a Docker bench, through a real MCP client (IMPLEMENTATION.md
§10; task R2).

The client runs the example scenario, waits for the run to end, and asks for each
report format once the bench has been removed. The JSON report names the images the run
used and the upstream revision, the JUnit XML has a case per step, and the summary is
readable. The images are named as for the run tests.
"""

import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp_run import (
    BENCH,
    SCENARIO,
    SCENARIO_NAME,
    SOURCE,
    call,
    images,
    until_ended,
)

from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.runtime.pin import UPSTREAM_REVISION
from igs_iec104_mcp.scenarios.runner import PROJECT_LABEL


async def test_the_reports_of_a_docker_run_survive_the_removal_of_its_bench(
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
        run_id = started["id"]
        record = await until_ended(session, run_id)
        assert record["status"] == "passed", record

        as_json = await call(session, "run_report", run_id=run_id, format="json")
        document = json.loads(as_json["content"])
        assert document["run"]["status"] == "passed"
        assert document["summary"]["passed"] == 6
        assert document["engine"] == {
            "runtime": runtime,
            "relay": relay,
            "upstream": UPSTREAM_REVISION,
        }
        assert document["cleanup"]["bench_stopped"] is True
        assert document["notes"] == [], "a healthy run drops no evidence"
        assert document["cleanup"]["bench"]["graceful"] is True
        assert "stop.json" in [e["name"] for e in document["manifest"]["evidence"]]

        as_junit = await call(session, "run_report", run_id=run_id, format="junit")
        suite = ET.fromstring(as_junit["content"].split("\n", 1)[1]).find("testsuite")
        assert suite is not None
        assert suite.get("tests") == "6"
        assert suite.get("failures") == "0" and suite.get("errors") == "0"

        as_summary = await call(session, "run_report", run_id=run_id, format="summary")
        assert f"- Upstream revision: {UPSTREAM_REVISION}" in as_summary["content"]

        again = await call(session, "run_report", run_id=run_id, format="json")
        assert again["sha256"] == as_json["sha256"], "the stored report is read back"

    owner = Ownership(project=PROJECT_LABEL, bench="basic-pair", run=run_id)
    assert await DockerAdapter.connect().owned(owner) == []
    assert (tmp_path / "runs" / run_id / "artifacts" / "report.json").is_file()
