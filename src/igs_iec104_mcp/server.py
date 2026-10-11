"""The MCP server of the test benches: the definition tools, over stdio.

Task M1 (IMPLEMENTATION.md §9). Stdout carries the MCP messages and nothing else, so
the server prints nothing to it: its logs go to stderr. A request that fails is a tool
error. The SDK puts `Error executing tool <name>: ` in front of its text, and the text
then starts with the stable error code of the service (`not_found: ...`). The tools are
thin: the business logic is in `definitions.service`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from igs_iec104_mcp.definitions import service
from igs_iec104_mcp.definitions.store import ProjectStore, StoreError

NAME = "igs-iec104-mcp"
INSTRUCTIONS = (
    "Manages the definitions of IEC 60870-5-104 test benches and their scenarios. "
    "Validate a definition before storing it. An update names the revision it was "
    "read at. A failed request says its error code first."
)


def _call[T](request: Callable[[], T]) -> T:
    """Runs a request of the service. A failure is a tool error, with its code first."""
    try:
        return request()
    except service.ServiceError as error:
        raise ToolError(str(error)) from error


def create_server(project: Path) -> MCPServer:
    """The server over the project directory. Raises StoreError when the directory does
    not exist."""
    store = ProjectStore(project)
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
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        server = create_server(arguments.project)
    except StoreError as error:
        print(f"{NAME}: {error}", file=sys.stderr)
        return 2
    server.run("stdio")
    return 0
