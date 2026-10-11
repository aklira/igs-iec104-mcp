"""A controller killed during a run (IMPLEMENTATION.md §10; task D3).

A controller process starts a run on a Docker bench, and is killed (SIGKILL) once the
run has passed its first step. A new controller then reconciles the store: the run is
marked interrupted, and its journal is kept. Its bench is still on the engine, so it is
listed, and removed on request, with the logs of its containers kept in the evidence.

The images are named by IGS_RUNTIME_IMAGE and IGS_RELAY_IMAGE. Without them the test
skips, except in CI, where a missing input fails the run.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from docker.errors import NotFound

from docker import from_env  # type: ignore[attr-defined]
from igs_iec104_mcp.definitions.service import create
from igs_iec104_mcp.definitions.store import ProjectStore
from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.models.run import RunStatus
from igs_iec104_mcp.scenarios.runner import PROJECT_LABEL, DockerEngine, Runner
from igs_iec104_mcp.storage.runs import RunStore

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src"
BENCH = (ROOT / "examples/benches/basic-pair.yaml").read_text(encoding="utf-8")
# The client starts its transfer, then waits for an update that never comes, so the run
# is still going when the controller is killed.
HOLD = """\
schema_version: 1
name: hold
bench: basic-pair
steps:
  - action: start_transfer
    client: client-1
    timeout: 10s
  - action: expect_point
    client: client-1
    address: 100
    kind: float
    value: 99.5
    timeout: 300s
"""
# The controller process: it starts the run, prints its identity, and waits.
CONTROLLER = """\
import asyncio, sys
from pathlib import Path
from igs_iec104_mcp.definitions.store import ProjectStore
from igs_iec104_mcp.scenarios.runner import DockerEngine, Runner
from igs_iec104_mcp.storage.runs import RunStore

async def main(project, image, relay):
    runner = Runner(
        ProjectStore(Path(project)),
        RunStore(Path(project)),
        DockerEngine(image=image, relay=relay),
    )
    started = await runner.start("hold")
    print(started.id, flush=True)
    await asyncio.sleep(3600)

asyncio.run(main(*sys.argv[1:4]))
"""
PATIENCE = 120.0
POLL = 0.2


@pytest.fixture
def runtime_image() -> str:
    return _identity("IGS_RUNTIME_IMAGE", "docker/runtime.Dockerfile")


@pytest.fixture
def relay_image() -> str:
    return _identity("IGS_RELAY_IMAGE", "docker/relay.Dockerfile")


def _identity(variable: str, dockerfile: str) -> str:
    reference = os.environ.get(variable)
    message = f"set {variable} to the image built from {dockerfile}"
    if not reference:
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    try:
        identity: str = from_env().images.get(reference).id
    except NotFound:
        pytest.fail(f"no image {reference!r} on the engine: {message}")
    return identity


async def until[T](check: Callable[[], Awaitable[T | None]], what: str) -> T:
    try:
        async with asyncio.timeout(PATIENCE):
            while True:
                value = await check()
                if value is not None:
                    return value
                await asyncio.sleep(POLL)
    except TimeoutError as error:
        raise AssertionError(f"timed out waiting for {what}") from error


def kinds(runs: RunStore, run_id: str) -> list[str]:
    return [event["kind"] for event in runs.events(run_id, limit=100).events]


async def test_a_run_of_a_killed_controller_is_interrupted_and_its_bench_removed(
    adapter: DockerAdapter,
    runtime_image: str,
    relay_image: str,
    tmp_path: Path,
) -> None:
    store = ProjectStore(tmp_path)
    runs = RunStore(tmp_path)
    create(store, "bench", "basic-pair", BENCH)
    create(store, "scenario", "hold", HOLD)
    log = (tmp_path / "controller.log").open("w", encoding="utf-8")
    controller = subprocess.Popen(
        [sys.executable, "-c", CONTROLLER, str(tmp_path), runtime_image, relay_image],
        env={**os.environ, "PYTHONPATH": str(SOURCE)},
        stdout=subprocess.PIPE,
        stderr=log,
        text=True,
    )
    run_id = ""
    try:
        assert controller.stdout is not None
        run_id = (await asyncio.to_thread(controller.stdout.readline)).strip()
        assert run_id, "the controller printed no run identity"

        async def first_step_passed() -> bool | None:
            events = runs.events(run_id, limit=10).events
            return True if any(e["kind"] == "verdict" for e in events) else None

        await until(first_step_passed, "the first step of the run")
    finally:
        controller.kill()
        controller.wait()
        log.close()

    owner = Ownership(project=PROJECT_LABEL, bench="basic-pair", run=run_id)
    assert await adapter.owned(owner), "the bench is still on the engine"
    try:
        runner = Runner(
            store, runs, DockerEngine(image=runtime_image, relay=relay_image)
        )
        assert runner.reconcile() == [run_id]
        record = runs.get(run_id)
        assert record.status is RunStatus.INTERRUPTED
        assert kinds(runs, run_id) == ["verdict", "interrupted"]

        page = await runner.recovered()
        assert [(b.run, b.status) for b in page.benches] == [(run_id, "interrupted")]

        removal = await runner.remove_recovered(run_id)
        assert removal.removed, "the bench's resources were removed"
        evidence = tmp_path / "runs" / run_id / "evidence"
        assert any(path.suffix == ".log" for path in evidence.iterdir())
        assert kinds(runs, run_id) == ["verdict", "interrupted"], "the journal is kept"
        assert await adapter.owned(owner) == []
        page = await runner.recovered()
        assert page.benches == []
    finally:
        await adapter.remove_owned(owner)
