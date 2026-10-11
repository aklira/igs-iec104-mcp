"""The MCP server of the test benches: the definition and run tools, over stdio.

Tasks M1 and M2 (IMPLEMENTATION.md §9). Stdout carries the MCP messages and nothing
else, so the server prints nothing to it: its logs go to stderr. A request that fails is
a tool error. The SDK puts `Error executing tool <name>: ` in front of its text, and the
text then starts with the stable error code of the service (`not_found: ...`). The tools
are thin: the business logic is in `definitions.service` and `scenarios.runner`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from igs_iec104_mcp.definitions import service, targets
from igs_iec104_mcp.definitions.store import ProjectStore, StoreError
from igs_iec104_mcp.scenarios.runner import (
    DockerEngine,
    EventsOut,
    NoEngine,
    RecoveredPage,
    RemovedOut,
    Runner,
    RunOut,
    RunsOut,
    run_out,
)
from igs_iec104_mcp.storage.runs import RunStore

NAME = "igs-iec104-mcp"
INSTRUCTIONS = (
    "Manages the definitions of IEC 60870-5-104 test benches and their scenarios, and "
    "runs the scenarios on Docker benches. Validate a definition before storing it. An "
    "update names the revision it was read at. A run is followed with run_get and "
    "run_events. A failed request says its error code first."
)


def _call[T](request: Callable[[], T]) -> T:
    """Runs a request of the service. A failure is a tool error, with its code first."""
    try:
        return request()
    except service.ServiceError as error:
        raise ToolError(str(error)) from error


async def _acall[T](request: Callable[[], Awaitable[T]]) -> T:
    """Runs an asynchronous request. A failure is a tool error, with its code first."""
    try:
        return await request()
    except service.ServiceError as error:
        raise ToolError(str(error)) from error


def create_server(
    project: Path,
    *,
    runtime_image: str | None = None,
    relay_image: str | None = None,
) -> MCPServer:
    """The server over the project directory. Runs need both images; without them, a run
    is refused with `engine_unavailable`. Raises StoreError when the directory does not
    exist."""
    store = ProjectStore(project)
    runs = RunStore(project)
    if runtime_image and relay_image:
        engine: DockerEngine | NoEngine = DockerEngine(
            image=runtime_image, relay=relay_image
        )
    else:
        engine = NoEngine()
    runner = Runner(store, runs, engine)
    interrupted = runner.reconcile()
    if interrupted:
        logging.getLogger(NAME).warning(
            "marked %d run(s) interrupted: %s", len(interrupted), ", ".join(interrupted)
        )
    server = MCPServer(NAME, instructions=INSTRUCTIONS)

    @server.tool(
        name="bench_validate",
        description="Validate the YAML of a bench without storing it. The result "
        "lists each problem with its path.",
    )
    def bench_validate(text: str) -> service.Validation:
        return service.validate("bench", text)

    @server.tool(
        name="bench_create",
        description="Store a new bench under a name. Refused when the name is taken.",
    )
    def bench_create(name: str, text: str) -> service.Definition:
        return _call(lambda: service.create(store, "bench", name, text))

    @server.tool(
        name="bench_get",
        description="The bench of a name, with its revision and its text.",
    )
    def bench_get(name: str) -> service.Definition:
        return _call(lambda: service.get(store, "bench", name))

    @server.tool(
        name="bench_list",
        description="A page of the bench names, in order. Pass next_cursor as the "
        "cursor of the next page.",
    )
    def bench_list(
        limit: int = service.DEFAULT_LIMIT, cursor: str | None = None
    ) -> service.Page:
        return _call(
            lambda: service.list_names(store, "bench", limit=limit, cursor=cursor)
        )

    @server.tool(
        name="scenario_validate",
        description="Validate the YAML of a scenario without storing it. It is also "
        "checked against its bench when the project has that bench.",
    )
    def scenario_validate(text: str) -> service.Validation:
        return service.validate("scenario", text, store)

    @server.tool(
        name="scenario_create",
        description="Store a new scenario under a name. Refused when the name is "
        "taken.",
    )
    def scenario_create(name: str, text: str) -> service.Definition:
        return _call(lambda: service.create(store, "scenario", name, text))

    @server.tool(
        name="scenario_update",
        description="Replace a scenario. The revision is the one it was read at: a "
        "scenario that changed since is not replaced.",
    )
    def scenario_update(name: str, text: str, revision: str) -> service.Definition:
        return _call(lambda: service.update(store, "scenario", name, text, revision))

    @server.tool(
        name="scenario_get",
        description="The scenario of a name, with its revision and its text.",
    )
    def scenario_get(name: str) -> service.Definition:
        return _call(lambda: service.get(store, "scenario", name))

    @server.tool(
        name="scenario_list",
        description="A page of the scenario names, in order. Pass next_cursor as the "
        "cursor of the next page.",
    )
    def scenario_list(
        limit: int = service.DEFAULT_LIMIT, cursor: str | None = None
    ) -> service.Page:
        return _call(
            lambda: service.list_names(store, "scenario", limit=limit, cursor=cursor)
        )

    @server.tool(
        name="scenario_run",
        description="Start a run of a scenario: its bench starts, the scenario runs, "
        "and the bench is removed after it. Returns the run at once, running. Follow "
        "it with run_get and run_events.",
    )
    async def scenario_run(name: str) -> RunOut:
        return run_out(await _acall(lambda: runner.start(name)))

    @server.tool(
        name="run_get",
        description="The record of a run: its status, its times, and the counts of its "
        "event journal.",
    )
    def run_get(run_id: str) -> RunOut:
        return _call(lambda: run_out(runner.get(run_id)))

    @server.tool(
        name="run_list",
        description="A page of the runs, in creation order. Pass next_cursor as the "
        "cursor of the next page.",
    )
    def run_list(limit: int = 50, cursor: str | None = None) -> RunsOut:
        page = _call(lambda: runner.list_runs(limit=limit, cursor=cursor))
        return RunsOut(
            items=[run_out(record) for record in page.items],
            next_cursor=None if page.next_cursor is None else str(page.next_cursor),
        )

    @server.tool(
        name="run_events",
        description="A page of the event journal of a run, in order: its verdicts as "
        "they are known, and its other events. Pass next_cursor as the cursor of the "
        "next page.",
    )
    def run_events(
        run_id: str, limit: int = 50, cursor: str | None = None
    ) -> EventsOut:
        page = _call(lambda: runner.events(run_id, limit=limit, cursor=cursor))
        return EventsOut(
            events=page.events,
            next_cursor=None if page.next_cursor is None else str(page.next_cursor),
        )

    @server.tool(
        name="run_cancel",
        description="Ask a running run to stop. It ends cancelled once its steps have "
        "stopped and its cleanup has run.",
    )
    async def run_cancel(run_id: str) -> RunOut:
        return run_out(await _acall(lambda: runner.cancel(run_id)))

    @server.tool(
        name="bench_recovered",
        description="List the benches a controller left on the engine: each names its "
        "run and its bench, and the run's status when it is known. A bench of a run "
        "this server runs is not listed.",
    )
    async def bench_recovered() -> RecoveredPage:
        return await _acall(lambda: runner.recovered())

    @server.tool(
        name="bench_recovered_remove",
        description="Remove the resources of a recovered bench, by its run. The logs "
        "of its containers are kept with the run's evidence. Refused for a run this "
        "server is running.",
    )
    async def bench_recovered_remove(run_id: str) -> RemovedOut:
        return await _acall(lambda: runner.remove_recovered(run_id))

    @server.tool(
        name="target_validate",
        description="Check an external target of a stored bench: its endpoint and the "
        "operations it allows, without connecting. With probe set, also try a TCP "
        "connection to its endpoint, which sends no IEC frame.",
    )
    async def target_validate(
        bench: str,
        target: str,
        probe: bool = False,
        timeout: float = targets.PROBE_DEFAULT_SECONDS,
    ) -> targets.TargetReport:
        return await _acall(
            lambda: targets.validate_target(
                store, bench, target, probe=probe, timeout=timeout
            )
        )

    return server


def main(argv: list[str] | None = None) -> int:
    """Runs the server over stdio, until its client closes the connection."""
    parser = argparse.ArgumentParser(
        prog=NAME,
        description="The MCP server of the IEC 60870-5-104 test benches, over stdio.",
    )
    parser.add_argument(
        "--project",
        type=Path,
        required=True,
        help="the project directory of the definitions, which must exist",
    )
    parser.add_argument(
        "--runtime-image",
        help="the runtime image of the benches, by its identity (a digest, not a tag)",
    )
    parser.add_argument(
        "--relay-image",
        help="the relay image of the benches, by its identity (a digest, not a tag)",
    )
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        server = create_server(
            arguments.project,
            runtime_image=arguments.runtime_image,
            relay_image=arguments.relay_image,
        )
    except StoreError as error:
        print(f"{NAME}: {error}", file=sys.stderr)
        return 2
    server.run("stdio")
    return 0
