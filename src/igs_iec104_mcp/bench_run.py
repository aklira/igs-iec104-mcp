"""The opt-in bench run of the preflight (IMPLEMENTATION.md §14, task I5; issue #1).

With `--run-bench`, the doctor runs the example scenario on a managed bench through the
server's run tools, on the engine the preflight found (Podman first). The bench starts
containers, so the run is opt-in. The run's cleanup removes the bench, and the doctor
then checks that nothing the run owns is left on the engine.

The definitions and the records are kept in a temporary project. It is removed when the
run passes; a run that does not pass keeps it, so that its evidence can be read.
"""

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

# The SDK ships no type hints (see pyproject.toml): its client is typed Any here.
from docker import DockerClient  # type: ignore[attr-defined]
from igs_iec104_mcp.checks import Check, Status
from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.handshake import EXAMPLE_BENCH, ROOT, describe, server_parameters
from igs_iec104_mcp.scenarios.runner import PROJECT_LABEL

EXAMPLE_SCENARIO = (
    ROOT / "examples" / "scenarios" / "interrogation-and-spontaneous-update.yaml"
)
BENCH_NAME = "basic-pair"
SCENARIO_NAME = "interrogation-and-spontaneous-update"
ENDED = frozenset({"passed", "failed", "cancelled", "error"})
RUN_TIMEOUT = 600.0
CANCEL_TIMEOUT = 120.0
POLL = 1.0
ENGINE_TIMEOUT = 5.0


class RunError(Exception):
    """A run tool refused a request, with the text of its error."""


@dataclass(frozen=True)
class Images:
    runtime: str
    relay: str


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: str
    passed: int
    total: int


def default_state_file() -> Path:
    """Where scripts/install.sh writes the identities of the images it built."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "igs-iec104-mcp" / "install.json"


def images_for(
    engine: str, runtime: str | None, relay: str | None, state_file: Path
) -> Images | str:
    """The identities of the run's images, or the reason there are none. Images given
    on the command line take precedence over the installer's state file."""
    state: dict[str, Any] = {}
    if runtime is None or relay is None:
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return (
                "no images are known: give --runtime-image and --relay-image, or run "
                f"scripts/install.sh --engine {engine}"
            )
        except (OSError, ValueError) as error:
            return f"the install state {state_file} cannot be read: {error}"
        if not isinstance(loaded, dict):
            return f"the install state {state_file} is not a JSON object"
        state = loaded
        if state.get("engine") != engine:
            return (
                f"the installed images are for {state.get('engine')}, and the engine "
                f"is {engine}: run scripts/install.sh --engine {engine}"
            )
    values = [runtime or state.get("runtime_image"), relay or state.get("relay_image")]
    if not all(isinstance(value, str) and value for value in values):
        return "the image identities are missing: run scripts/install.sh"
    return Images(runtime=str(values[0]), relay=str(values[1]))


def bench_parameters(project: Path, images: Images, host: str) -> StdioServerParameters:
    """The server of the handshake, with the images of the run, on the engine the
    preflight found."""
    base = server_parameters(project)
    environment = dict(base.env or {})
    environment["DOCKER_HOST"] = host
    return StdioServerParameters(
        command=base.command,
        args=[
            *base.args,
            "--runtime-image",
            images.runtime,
            "--relay-image",
            images.relay,
        ],
        env=environment,
    )


async def _call(session: ClientSession, tool: str, **arguments: Any) -> dict[str, Any]:
    result: CallToolResult = await session.call_tool(tool, arguments)
    if result.is_error:
        block = result.content[0]
        text = block.text if isinstance(block, TextContent) else str(block)
        raise RunError(f"{tool}: {text}")
    return dict(result.structured_content or {})


async def _until_ended(session: ClientSession, run_id: str) -> str:
    while True:
        record = await _call(session, "run_get", run_id=run_id)
        if record["status"] in ENDED:
            return str(record["status"])
        await asyncio.sleep(POLL)


async def _exchange(
    parameters: StdioServerParameters,
    scenario_text: str,
    errlog: TextIO,
) -> RunOutcome:
    bench_text = EXAMPLE_BENCH.read_text(encoding="utf-8")
    async with (
        stdio_client(parameters, errlog=errlog) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        await _call(session, "bench_create", name=BENCH_NAME, text=bench_text)
        await _call(session, "scenario_create", name=SCENARIO_NAME, text=scenario_text)
        started = await _call(session, "scenario_run", name=SCENARIO_NAME)
        run_id = str(started["id"])
        try:
            async with asyncio.timeout(RUN_TIMEOUT):
                status = await _until_ended(session, run_id)
        except TimeoutError:
            # The run is stopped, so that its bench is removed before the report.
            await _call(session, "run_cancel", run_id=run_id)
            async with asyncio.timeout(CANCEL_TIMEOUT):
                status = await _until_ended(session, run_id)
        events = await _call(session, "run_events", run_id=run_id, limit=200)
        verdicts = [e for e in events["events"] if e["kind"] == "verdict"]
        passed = sum(1 for verdict in verdicts if verdict["outcome"] == "passed")
        return RunOutcome(
            run_id=run_id, status=status, passed=passed, total=len(verdicts)
        )


def _owned_resources(host: str, run_id: str) -> int:
    adapter = DockerAdapter(DockerClient(base_url=host, timeout=ENGINE_TIMEOUT))
    owner = Ownership(project=PROJECT_LABEL, bench=BENCH_NAME, run=run_id)
    return len(asyncio.run(adapter.owned(owner)))


def _scenario_text(override: str | None) -> str:
    return (
        override
        if override is not None
        else EXAMPLE_SCENARIO.read_text(encoding="utf-8")
    )


ParametersFor = Callable[[Path, Images, str], StdioServerParameters]


def bench_checks(
    engine: Check | None,
    runtime_image: str | None,
    relay_image: str | None,
    *,
    state_file: Path | None = None,
    parameters_for: ParametersFor = bench_parameters,
    scenario_text: str | None = None,
) -> list[Check]:
    """Runs the example scenario and checks its verdict and its cleanup. The arguments
    default to the installer's images and to the example scenario."""
    if engine is None or engine.status is Status.FAIL:
        return [
            Check(
                "bench-run",
                Status.FAIL,
                "no container engine to run the bench on",
                "fix the engine check, then run the bench check again",
            )
        ]
    kind = engine.data.get("engine", "")
    host = engine.data.get("host", "")
    images = images_for(
        kind, runtime_image, relay_image, state_file or default_state_file()
    )
    if isinstance(images, str):
        return [Check("bench-run", Status.FAIL, images, "")]

    project = Path(tempfile.mkdtemp(prefix="igs-doctor-run-"))
    scenario = _scenario_text(scenario_text)
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
            parameters = parameters_for(project, images, host)
            outcome = asyncio.run(
                asyncio.wait_for(
                    _exchange(parameters, scenario, errlog),
                    RUN_TIMEOUT + CANCEL_TIMEOUT,
                )
            )
    except Exception as error:
        kept = f"; the temporary project is kept at {project}"
        return [
            Check(
                "bench-run",
                Status.FAIL,
                f"the run did not complete: {describe(error)}{kept}",
                "run the server by hand with the same images (the doctor's handshake "
                "check names the command), and read its output",
            )
        ]

    data = {
        "run_id": outcome.run_id,
        "status": outcome.status,
        "verdicts": f"{outcome.passed}/{outcome.total}",
        "project": str(project),
    }
    if (
        outcome.status == "passed"
        and outcome.total > 0
        and outcome.passed == outcome.total
    ):
        run_check = Check(
            "bench-run",
            Status.OK,
            f"run {outcome.run_id} passed: {outcome.passed} of {outcome.total} "
            "verdicts",
            data=data,
        )
        shutil.rmtree(project, ignore_errors=True)
    else:
        run_check = Check(
            "bench-run",
            Status.FAIL,
            f"run {outcome.run_id} ended {outcome.status}: {outcome.passed} of "
            f"{outcome.total} verdicts passed; its evidence is kept in {project}",
            "read the verdicts with run_events and the report with run_report "
            "(through the MCP server, with the project above)",
            data=data,
        )

    try:
        leftover = _owned_resources(host, outcome.run_id)
    except Exception as error:
        cleanup = Check(
            "bench-cleanup",
            Status.FAIL,
            f"the engine could not be asked for the bench: {describe(error)}",
            "check the engine, then list the bench with bench_recovered",
        )
    else:
        if leftover:
            cleanup = Check(
                "bench-cleanup",
                Status.FAIL,
                f"{leftover} resource(s) of run {outcome.run_id} are left on the "
                "engine",
                "list them with bench_recovered, then remove them with "
                "bench_recovered_remove",
            )
        else:
            cleanup = Check(
                "bench-cleanup",
                Status.OK,
                f"no resource of run {outcome.run_id} is left on the engine",
            )
    return [run_check, cleanup]
