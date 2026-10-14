"""The handshake of the preflight without a server process: the server's parameters,
the errors of an exchange, and the validation of the example bench's result (I3)."""

import os
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from mcp.types import CallToolResult, TextContent

from igs_iec104_mcp.checks import Status
from igs_iec104_mcp.handshake import (
    SOURCE,
    describe,
    run_server_checks,
    server_parameters,
    validation_check,
)


def test_the_server_starts_as_this_interpreter_with_the_source_on_its_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/already/there")
    parameters = server_parameters(Path("/tmp/project"))
    assert parameters.command == sys.executable
    assert parameters.args == [
        "-m",
        "igs_iec104_mcp",
        "--project",
        str(Path("/tmp/project")),
    ]
    assert parameters.env is not None
    assert parameters.env["PYTHONPATH"] == os.pathsep.join(
        [str(SOURCE), "/already/there"]
    )


def test_an_exception_group_is_described_by_its_members() -> None:
    group = ExceptionGroup("group", [RuntimeError("first"), RuntimeError("second")])
    assert describe(group) == "first; second"
    assert describe(RuntimeError()) == "RuntimeError"


def test_a_valid_example_bench_passes() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="{}")],
        structured_content={"valid": True, "issues": []},
    )
    check = validation_check(result)
    assert check.status is Status.OK


def test_an_invalid_bench_names_each_issue_path() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="{}")],
        structured_content={
            "valid": False,
            "issues": [{"path": "colour", "message": "extra field"}],
        },
    )
    check = validation_check(result)
    assert check.status is Status.FAIL
    assert "colour: extra field" in check.detail
    assert "update the checkout" in check.action


def test_a_tool_error_shows_its_text() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="the tool crashed")],
        is_error=True,
    )
    check = validation_check(result)
    assert check.status is Status.FAIL
    assert "the tool crashed" in check.detail


def test_a_server_that_cannot_start_fails_with_the_corrective_step() -> None:
    def missing(project: Path) -> StdioServerParameters:
        return StdioServerParameters(
            command="/nonexistent/igs-doctor-test-program", args=[]
        )

    checks = run_server_checks(parameters_for=missing, timeout=30)
    assert [check.name for check in checks] == ["server"]
    assert checks[0].status is Status.FAIL
    assert "did not complete the handshake" in checks[0].detail
    assert "uv sync --locked" in checks[0].action
