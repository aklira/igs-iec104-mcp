"""A controller that stopped during a run (IMPLEMENTATION.md §10; task D3).

The runs the store holds as unended, when a controller starts, are marked interrupted:
their journal is kept, nothing of them is resumed, and no bench is started. The benches
the engine still holds for those runs are listed and removed on request, with the run's
evidence; a bench of a run this server runs is neither listed nor removed.
"""

import secrets
from pathlib import Path

import pytest
from test_scenario_runner import FakeEngine, kinds, project, settled

from igs_iec104_mcp.definitions.service import ServiceError
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.models.run import RunStatus
from igs_iec104_mcp.scenarios.runner import NoEngine, Runner
from igs_iec104_mcp.storage.runs import RunStore


class OwnersEngine(FakeEngine):
    """A fake engine that also holds benches of earlier controllers, and records the
    removals asked of it."""

    def __init__(self, benches: list[Ownership]) -> None:
        super().__init__(transfer="stopped")
        self.benches = benches
        self.removals: list[tuple[Ownership, Path | None]] = []

    async def owners(self) -> list[Ownership]:
        return list(self.benches)

    async def recover(self, owner: Ownership, evidence: Path | None) -> list[str]:
        self.removals.append((owner, evidence))
        return [f"igsmcp-{owner.run[:12]}-server-1"]


def owner_of(run_id: str) -> Ownership:
    return Ownership(project="igs-mcp", bench="basic-pair", run=run_id)


def leave_run(runs: RunStore, status: RunStatus) -> str:
    """A run as a controller that stopped leaves it: pending, or running with a verdict
    in its journal."""
    run_id = secrets.token_hex(16)
    runs.create(run_id, "basic-pair", "interrogate", 1_000)
    if status is RunStatus.RUNNING:
        runs.move(run_id, RunStatus.RUNNING)
        runs.record(
            run_id,
            "verdict",
            {
                "step": "step 1: wait_transfer_started",
                "outcome": "passed",
                "detail": "",
            },
        )
    return run_id


def test_a_running_run_left_by_a_stopped_controller_is_interrupted(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    run_id = leave_run(runs, RunStatus.RUNNING)
    engine = OwnersEngine([])
    interrupted = Runner(store, runs, engine).reconcile()
    assert interrupted == [run_id]
    record = runs.get(run_id)
    assert record.status is RunStatus.INTERRUPTED
    assert record.ended_at_ms is not None
    assert kinds(runs, run_id) == ["verdict", "interrupted"], "the journal is kept"
    assert engine.handles == [], "no bench was started and no command was sent"


def test_a_pending_run_left_by_a_stopped_controller_is_interrupted(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    run_id = leave_run(runs, RunStatus.PENDING)
    assert Runner(store, runs, OwnersEngine([])).reconcile() == [run_id]
    assert runs.get(run_id).status is RunStatus.INTERRUPTED


async def test_a_run_this_server_runs_is_not_reconciled(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, OwnersEngine([]))
    started = await runner.start("interrogate")
    assert runner.reconcile() == []
    assert runs.get(started.id).status is RunStatus.RUNNING
    await runner.cancel(started.id)
    assert (await settled(runner, started.id)).status is RunStatus.CANCELLED


async def test_a_run_that_has_ended_is_left_as_it_ended(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine())
    started = await runner.start("interrogate")
    await settled(runner, started.id)
    assert runner.reconcile() == []
    assert runs.get(started.id).status is RunStatus.PASSED


async def test_recovered_lists_the_benches_no_task_of_the_server_runs(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    left = leave_run(runs, RunStatus.RUNNING)
    unknown = secrets.token_hex(16)
    engine = OwnersEngine([owner_of(left), owner_of(unknown)])
    runner = Runner(store, runs, engine)
    runner.reconcile()
    page = await runner.recovered()
    assert [(b.run, b.bench, b.status) for b in page.benches] == [
        (left, "basic-pair", "interrupted"),
        (unknown, "basic-pair", None),
    ]


async def test_removing_a_recovered_bench_names_its_evidence(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    left = leave_run(runs, RunStatus.RUNNING)
    engine = OwnersEngine([owner_of(left)])
    runner = Runner(store, runs, engine)
    runner.reconcile()
    removed = await runner.remove_recovered(left)
    assert removed.removed == [f"igsmcp-{left[:12]}-server-1"]
    (owner, evidence), *_ = engine.removals
    assert owner == owner_of(left)
    assert evidence == tmp_path / "runs" / left / "evidence"
    assert runs.get(left).status is RunStatus.INTERRUPTED, "the run record is kept"


async def test_a_bench_of_a_run_this_server_runs_is_refused(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    engine = OwnersEngine([])
    runner = Runner(store, runs, engine)
    started = await runner.start("interrogate")
    engine.benches.append(owner_of(started.id))
    with pytest.raises(ServiceError) as error:
        await runner.remove_recovered(started.id)
    assert error.value.code == "invalid_state"
    assert engine.removals == []
    await runner.cancel(started.id)
    await settled(runner, started.id)


async def test_a_run_with_no_bench_on_the_engine_is_not_found(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, OwnersEngine([]))
    with pytest.raises(ServiceError) as error:
        await runner.remove_recovered(secrets.token_hex(16))
    assert error.value.code == "not_found"


async def test_without_an_engine_no_bench_is_listed_or_removed(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, NoEngine())
    with pytest.raises(ServiceError) as listing:
        await runner.recovered()
    assert listing.value.code == "engine_unavailable"
    with pytest.raises(ServiceError) as removal:
        await runner.remove_recovered(secrets.token_hex(16))
    assert removal.value.code == "engine_unavailable"
