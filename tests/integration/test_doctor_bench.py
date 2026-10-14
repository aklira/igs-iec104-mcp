"""The doctor's opt-in bench run on a real engine, Podman first (IMPLEMENTATION.md §14,
task I5; issue #1).

The images are named by IGS_RUNTIME_IMAGE and IGS_RELAY_IMAGE, as in test_mcp_run.py: a
tag or an identity, resolved on the engine the preflight finds. Without an engine, or
without the images, the tests skip, except in CI, where they fail.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from docker.errors import NotFound

# The SDK ships no type hints (see pyproject.toml): its client is typed Any here.
from docker import DockerClient  # type: ignore[attr-defined]
from igs_iec104_mcp.bench_run import EXAMPLE_SCENARIO, bench_checks
from igs_iec104_mcp.checks import Check, Status
from igs_iec104_mcp.doctor import SystemProbe, run_checks

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def _missing(message: str) -> None:
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture
def target() -> Iterator[tuple[Check, str, str]]:
    """The engine check and the identities of the two images on that engine."""
    engine = next(
        check
        for check in run_checks(SystemProbe(), engine="auto").checks
        if check.name == "engine"
    )
    if engine.status is Status.FAIL:
        _missing(f"no container engine: {engine.detail}")
    names = [os.environ.get("IGS_RUNTIME_IMAGE"), os.environ.get("IGS_RELAY_IMAGE")]
    if not all(names):
        _missing("set IGS_RUNTIME_IMAGE and IGS_RELAY_IMAGE to the built images")
    client = DockerClient(base_url=engine.data["host"], timeout=10)
    try:
        identities = []
        for name in names:
            try:
                identities.append(str(client.images.get(name).id))
            except NotFound:
                pytest.fail(f"no image {name!r} on the engine")
    finally:
        client.close()
    yield engine, identities[0], identities[1]


def _temporary_runs() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("igs-doctor-run-*"))


def test_the_example_run_passes_and_leaves_nothing_on_the_engine(
    target: tuple[Check, str, str],
) -> None:
    engine, runtime, relay = target
    before = _temporary_runs()
    run, cleanup = bench_checks(engine, runtime, relay)
    assert run.status is Status.OK, run.detail
    assert "6 of 6 verdicts" in run.detail
    assert cleanup.status is Status.OK, cleanup.detail
    assert _temporary_runs() == before, "a passed run removes its temporary project"


def test_a_run_that_fails_removes_its_bench_and_keeps_its_evidence(
    target: tuple[Check, str, str],
) -> None:
    engine, runtime, relay = target
    scenario = EXAMPLE_SCENARIO.read_text(encoding="utf-8")
    wrong = scenario.replace("value: 12.5", "value: 99.0", 1)
    assert wrong != scenario
    run, cleanup = bench_checks(engine, runtime, relay, scenario_text=wrong)
    kept = Path(run.data["project"])
    try:
        assert run.status is Status.FAIL, run.detail
        assert "ended failed" in run.detail
        assert (kept / "runs" / run.data["run_id"]).is_dir(), "evidence is kept"
        assert cleanup.status is Status.OK, cleanup.detail
    finally:
        shutil.rmtree(kept, ignore_errors=True)


def test_the_doctor_entry_point_runs_the_bench(
    target: tuple[Check, str, str],
) -> None:
    _, runtime, relay = target
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "igs_iec104_mcp.doctor",
            "--run-bench",
            "--json",
            "--runtime-image",
            runtime,
            "--relay-image",
            relay,
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(SRC)),
        timeout=900,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    statuses = {
        item["name"]: item["status"] for item in json.loads(done.stdout)["checks"]
    }
    assert statuses["bench-run"] == "ok"
    assert statuses["bench-cleanup"] == "ok"
