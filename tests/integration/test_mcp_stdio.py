"""The MCP server over stdio, through a real MCP client.

Task M1 (IMPLEMENTATION.md §9). The client starts the server as a process, lists its
tools, and calls the definition tools. Stdout carries only MCP messages, so the protocol
would fail if the server printed anything else to it. No runtime or Docker engine is
needed.
"""

import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src"
BENCH = (ROOT / "examples/benches/basic-pair.yaml").read_text(encoding="utf-8")
TOOLS = {
    "bench_validate",
    "bench_create",
    "bench_get",
    "bench_list",
    "scenario_validate",
    "scenario_create",
    "scenario_update",
    "scenario_get",
    "scenario_list",
    "scenario_run",
    "run_get",
    "run_list",
    "run_events",
    "run_cancel",
    "run_report",
    "run_prune",
    "target_validate",
    "bench_recovered",
    "bench_recovered_remove",
}


def server_parameters(project: Path) -> StdioServerParameters:
    """The server as a process: the project is not installed, so its source is on the
    path of the process."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "igs_iec104_mcp", "--project", str(project)],
        env={"PYTHONPATH": str(SOURCE)},
    )


def error_code(result: CallToolResult) -> str:
    """The stable code of a tool error. The SDK puts `Error executing tool <name>: ` in
    front of the message, and the code is the first word of the message after it."""
    block = result.content[0]
    assert isinstance(block, TextContent)
    _, _, message = block.text.partition(": ")
    return message.partition(": ")[0]


async def test_an_mcp_client_lists_the_definition_tools_and_validates_a_bench(
    tmp_path: Path,
) -> None:
    async with (
        stdio_client(server_parameters(tmp_path)) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        assert {tool.name for tool in listed.tools} == TOOLS

        valid = await session.call_tool("bench_validate", {"text": BENCH})
        assert valid.is_error is False
        assert valid.structured_content == {"valid": True, "issues": []}

        invalid = await session.call_tool(
            "bench_validate", {"text": "colour: red\n" + BENCH}
        )
        assert invalid.is_error is False
        assert invalid.structured_content["valid"] is False
        assert invalid.structured_content["issues"][0]["path"] == "colour"


async def test_a_stored_bench_is_read_back_and_a_repeated_create_is_refused(
    tmp_path: Path,
) -> None:
    async with (
        stdio_client(server_parameters(tmp_path)) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        created = await session.call_tool(
            "bench_create", {"name": "basic-pair", "text": BENCH}
        )
        assert created.is_error is False
        assert created.structured_content["name"] == "basic-pair"

        again = await session.call_tool(
            "bench_create", {"name": "basic-pair", "text": BENCH}
        )
        assert again.is_error is True
        assert error_code(again) == "already_exists"

        fetched = await session.call_tool("bench_get", {"name": "basic-pair"})
        assert fetched.structured_content == created.structured_content

        missing = await session.call_tool("scenario_get", {"name": "nothing"})
        assert missing.is_error is True
        assert error_code(missing) == "not_found"

        listed = await session.call_tool("bench_list", {})
        assert listed.structured_content == {
            "items": ["basic-pair"],
            "next_cursor": None,
        }
