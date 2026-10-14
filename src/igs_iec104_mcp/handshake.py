"""The MCP handshake of the preflight (IMPLEMENTATION.md §14, task I3; issue #1).

The doctor starts the server the way an MCP client does: over stdio, as a process of
this interpreter, against a temporary project that is removed afterwards. It checks the
handshake, the tool list, and one real tool call: `bench_validate` on the example bench.
No container engine is needed, and no bench is stored or started.
"""

import asyncio
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

from igs_iec104_mcp.checks import Check, Status

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).resolve().parents[1]
EXAMPLE_BENCH = ROOT / "examples" / "benches" / "basic-pair.yaml"
REQUIRED_TOOL = "bench_validate"
HANDSHAKE_TIMEOUT = 60.0

ParametersFor = Callable[[Path], StdioServerParameters]


def server_parameters(project: Path) -> StdioServerParameters:
    """The server as a client starts it. The project is not installed, so the source
    directory is put on the path of the process."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(SOURCE), environment.get("PYTHONPATH")) if part
    )
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "igs_iec104_mcp", "--project", str(project)],
        env=environment,
    )


def describe(error: BaseException) -> str:
    """The message of an error, with the errors of an exception group joined."""
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(describe(item) for item in error.exceptions)
    return str(error) or type(error).__name__


def validation_check(result: CallToolResult) -> Check:
    payload = result.structured_content or {}
    if not result.is_error and payload.get("valid") is True:
        return Check("bench-validate", Status.OK, "the example bench validates")
    issues = [
        f"{issue['path']}: {issue['message']}"
        for issue in payload.get("issues") or []
        if isinstance(issue, dict)
    ]
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    detail = "; ".join(issues or texts) or "the server refused the example bench"
    return Check(
        "bench-validate",
        Status.FAIL,
        f"{REQUIRED_TOOL} did not accept the example bench: {detail}",
        "the server and the example bench of this checkout disagree: update the "
        "checkout, or report the problem with the output above",
    )


async def exchange(
    parameters: StdioServerParameters, bench_text: str, errlog: TextIO
) -> list[Check]:
    async with (
        stdio_client(parameters, errlog=errlog) as (read, write),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        info = initialized.server_info
        checks = [
            Check(
                "server",
                Status.OK,
                f"{' '.join(part for part in (info.name, info.version) if part)} "
                "answered the handshake",
            )
        ]
        listed = await session.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        if REQUIRED_TOOL not in names:
            checks.append(
                Check(
                    "tools",
                    Status.FAIL,
                    f"{len(names)} tools listed; {REQUIRED_TOOL} is not one of them",
                    "the server of this checkout is not the one the bench needs: "
                    "update the checkout",
                )
            )
            return checks
        checks.append(Check("tools", Status.OK, f"{len(names)} tools listed"))
        result = await session.call_tool(REQUIRED_TOOL, {"text": bench_text})
        checks.append(validation_check(result))
        return checks


def _server_failure(reason: str, errlog: TextIO) -> Check:
    errlog.seek(0)
    lines = errlog.read().strip().splitlines()
    last = f"; the server said: {lines[-1]}" if lines else ""
    return Check(
        "server",
        Status.FAIL,
        f"the server did not complete the handshake: {reason}{last}",
        "run the server by hand (python -m igs_iec104_mcp --project DIR, with "
        "PYTHONPATH set to src) to see its error, and check the environment with "
        "uv sync --locked",
    )


def run_server_checks(
    *,
    parameters_for: ParametersFor = server_parameters,
    timeout: float = HANDSHAKE_TIMEOUT,
) -> list[Check]:
    """Starts the server against a temporary project and runs the handshake. The
    arguments default to the server of this checkout."""
    if not EXAMPLE_BENCH.is_file():
        return [
            Check(
                "bench-validate",
                Status.FAIL,
                f"the example bench is missing: {EXAMPLE_BENCH}",
                "run the doctor from the source tree of the checkout",
            )
        ]
    bench_text = EXAMPLE_BENCH.read_text(encoding="utf-8")
    with (
        tempfile.TemporaryDirectory(prefix="igs-doctor-") as project,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog,
    ):
        parameters = parameters_for(Path(project))
        try:
            return asyncio.run(
                asyncio.wait_for(exchange(parameters, bench_text, errlog), timeout)
            )
        except TimeoutError:
            return [_server_failure(f"no answer within {timeout:g} s", errlog)]
        except Exception as error:
            return [_server_failure(describe(error), errlog)]
