"""The doctor's handshake against the real server, through the package's own entry
point (IMPLEMENTATION.md §14, task I3; issue #1).

No container engine is needed: the handshake lists the tools and validates the example
bench, which stores nothing and starts no bench.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from igs_iec104_mcp.checks import Check
from igs_iec104_mcp.doctor import main
from igs_iec104_mcp.handshake import run_server_checks

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src"


def test_the_doctor_entry_point_passes_the_handshake_of_this_checkout() -> None:
    environment = dict(os.environ, PYTHONPATH=str(SOURCE))
    done = subprocess.run(
        [sys.executable, "-m", "igs_iec104_mcp.doctor", "--server-only", "--json"],
        capture_output=True,
        text=True,
        env=environment,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["ok"] is True
    statuses = {item["name"]: item["status"] for item in payload["checks"]}
    assert statuses == {"server": "ok", "tools": "ok", "bench-validate": "ok"}


def test_a_wrong_command_path_exits_with_1_and_names_the_corrective_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(project: Path) -> StdioServerParameters:
        return StdioServerParameters(
            command="/nonexistent/igs-doctor-wrong-path", args=[]
        )

    def server() -> list[Check]:
        return run_server_checks(parameters_for=broken, timeout=60)

    assert main(["--server-only"], server=server) == 1
    out = capsys.readouterr().out
    assert "FAIL  server" in out
    assert "-> run the server by hand" in out
