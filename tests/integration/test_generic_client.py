"""The generic mode's entry starts the server. The printed configuration, read back
the way a client reads it, runs the handshake of the preflight (I7)."""

import json
import os
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from igs_iec104_mcp.checks import Status
from igs_iec104_mcp.handshake import run_server_checks
from igs_iec104_mcp.registration import main

ROOT = Path(__file__).resolve().parents[2]


def test_the_printed_entry_starts_the_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    code = main(
        [
            "--client",
            "generic",
            "--repository",
            str(ROOT),
            "--project-dir",
            str(project),
            "--runtime-image",
            "sha256:" + "a" * 64,
            "--relay-image",
            "sha256:" + "b" * 64,
            "--docker-host",
            "unix:///var/run/docker.sock",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    entry = json.loads(out[out.index("{") :])["mcpServers"]["igs-iec104"]
    environment = dict(os.environ)
    environment.update(entry["env"])
    parameters = StdioServerParameters(
        command=entry["command"], args=entry["args"], env=environment
    )
    checks = run_server_checks(parameters_for=lambda _project: parameters)
    assert [check.status for check in checks] == [Status.OK] * 3, checks
