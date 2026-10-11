"""Milestone 1 (IMPLEMENTATION.md §13), run with two runtimes from Python.

The runtime binary is named by IGS_RUNTIME_BINARY.
Without it the test is skipped, except in CI (where CI is set): there it fails,
so that a missing binary cannot pass unnoticed.
"""

from pathlib import Path

from igs_iec104_mcp.runtime.milestone1 import run_milestone_one


async def test_milestone_one_passes_with_two_local_runtimes(
    runtime_binary: Path, tmp_path: Path
) -> None:
    report = await run_milestone_one(runtime_binary, tmp_path)

    details = [f"{o.step}: {o.detail}" for o in report.observations]
    assert report.passed, (
        f"{details}; stops {report.station_stop}, {report.client_stop}"
    )
    assert {"connect", "start_transfer", "interrogate", "update_point"} <= set(
        report.step_seconds
    )
    assert "asdu" in report.event_kinds
    assert "transfer" in report.event_kinds
