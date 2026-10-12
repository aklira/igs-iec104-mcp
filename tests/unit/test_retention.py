"""The limits and the retention of the runs (IMPLEMENTATION.md §6, §7, §10; task R3).

A run whose runtime's log dropped events says so: the drop is a gap in its evidence,
and its report lists it. The retention policy chooses which ended runs go, by count and
by age, and never a run that has not ended or whose bench is still on the engine. A
removed run leaves no record, journal, artifact, or evidence behind.
"""

import json
import secrets
from pathlib import Path
from typing import Any

import pytest
from test_scenario_runner import (
    FakeClient,
    FakeEngine,
    FakeHandle,
    kinds,
    project,
    settled,
)

from igs_iec104_mcp.definitions.service import ServiceError
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.models.run import RunStatus
from igs_iec104_mcp.reporting.reports import report_of
from igs_iec104_mcp.scenarios.runner import Runner
from igs_iec104_mcp.storage.retention import RetentionPolicy
from igs_iec104_mcp.storage.runs import RunError, RunRecord

DAY_MS = 86_400_000


def ended(
    run_id: str, created: int, ended_at: int | None, status: RunStatus
) -> RunRecord:
    return RunRecord(
        id=run_id,
        bench="basic-pair",
        scenario="interrogate",
        status=status,
        created_at_ms=created,
        ended_at_ms=ended_at,
        events=0,
        dropped=0,
    )


def test_a_policy_keeps_something_and_names_what_it_keeps() -> None:
    with pytest.raises(ValueError, match="keeps runs"):
        RetentionPolicy()
    with pytest.raises(ValueError, match="zero or more"):
        RetentionPolicy(keep_runs=-1)
    with pytest.raises(ValueError, match="more than zero"):
        RetentionPolicy(keep_days=0)
    assert RetentionPolicy(keep_runs=2, keep_days=3).describe() == (
        "keeps the newest 2 ended runs and runs that ended within 3 days"
    )


def test_the_policy_removes_the_oldest_ended_runs_beyond_the_count() -> None:
    runs = [
        ended("a" * 32, 1, 2, RunStatus.PASSED),
        ended("b" * 32, 3, 4, RunStatus.FAILED),
        ended("c" * 32, 5, 6, RunStatus.ERROR),
        ended("d" * 32, 7, None, RunStatus.RUNNING),
    ]
    chosen = RetentionPolicy(keep_runs=1).select(runs, now_ms=100)
    assert [r.id for r in chosen] == ["a" * 32, "b" * 32]


def test_the_policy_removes_the_runs_that_ended_before_the_age_limit() -> None:
    now = 10 * DAY_MS
    runs = [
        ended("a" * 32, 1, DAY_MS, RunStatus.PASSED),
        ended("b" * 32, 2, 9 * DAY_MS, RunStatus.PASSED),
    ]
    chosen = RetentionPolicy(keep_days=2).select(runs, now_ms=now)
    assert [r.id for r in chosen] == ["a" * 32]


async def test_a_prune_without_a_policy_is_refused(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine())
    with pytest.raises(ServiceError) as error:
        await runner.prune()
    assert error.value.code == "invalid_argument"


async def finished_run(runner: Runner, name: str = "interrogate") -> str:
    started = await runner.start(name)
    await settled(runner, started.id)
    return started.id


async def test_a_dry_run_says_what_would_go_and_removes_nothing(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine(), RetentionPolicy(keep_runs=1))
    older = await finished_run(runner)
    newer = await finished_run(runner)
    result = await runner.prune(dry_run=True)
    assert result.dry_run is True
    assert result.removed == [older]
    assert result.kept_for_bench == []
    assert runs.get(older).status is RunStatus.PASSED, "nothing was removed"
    assert newer  # the newest run is kept


async def test_a_prune_removes_the_run_its_journal_and_its_artifacts(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine(), RetentionPolicy(keep_runs=0))
    run_id = await finished_run(runner)
    report_of(runs, run_id, "json")  # an artifact the run keeps
    assert (tmp_path / "runs" / run_id).is_dir()

    result = await runner.prune(dry_run=False)
    assert result.removed == [run_id]
    assert not (tmp_path / "runs" / run_id).exists()
    assert runs.list_runs(limit=10).items == []
    with pytest.raises(ServiceError) as error:
        runner.get(run_id)
    assert error.value.code == "not_found"


async def test_a_run_whose_bench_is_still_on_the_engine_is_kept(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    engine = OwnedEngine()
    runner = Runner(store, runs, engine, RetentionPolicy(keep_runs=0))
    run_id = await finished_run(runner)
    engine.benches = [Ownership(project="igs-mcp", bench="basic-pair", run=run_id)]
    result = await runner.prune(dry_run=False)
    assert result.removed == []
    assert result.kept_for_bench == [run_id]
    assert runs.get(run_id).status is RunStatus.PASSED


def test_only_an_ended_run_can_be_deleted(tmp_path: Path) -> None:
    _, runs = project(tmp_path)
    run_id = secrets.token_hex(16)
    runs.create(run_id, "basic-pair", "interrogate", 1_000)
    with pytest.raises(RunError, match="only an ended run is removed"):
        runs.delete(run_id)
    assert runs.get(run_id).status is RunStatus.PENDING


class DroppingClient(FakeClient):
    """A runtime whose log dropped events, and whose station events cannot be read."""

    async def events(self, after: int = 0) -> dict[str, Any]:
        body = await super().events(after)
        body["dropped"] = 2
        return body

    async def station_events(self, after: int = 0) -> dict[str, Any]:
        raise ConnectionError("the station is down")


class DroppingHandle(FakeHandle):
    def __init__(self, engine: FakeEngine) -> None:
        super().__init__(engine)
        self.client_api = DroppingClient(transfer=engine.transfer)


class DroppingEngine(FakeEngine):
    async def start(
        self, bench: Any, run_id: str, evidence: Path, material: Any
    ) -> DroppingHandle:
        handle = DroppingHandle(self)
        self.handles.append(handle)
        return handle


class OwnedEngine(FakeEngine):
    """An engine that still holds the benches of the given runs."""

    def __init__(self) -> None:
        super().__init__()
        self.benches: list[Ownership] = []

    async def owners(self) -> list[Ownership]:
        return list(self.benches)


async def test_drops_and_unreadable_counts_are_gaps_in_the_run(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, DroppingEngine())
    run_id = await finished_run(runner)
    gaps = [e for e in runs.events(run_id).events if e["kind"] == "gap"]
    by_instance = {g["instance"]: g for g in gaps}
    assert by_instance["client-1"]["dropped"] == 2
    assert "dropped 2 events" in by_instance["client-1"]["detail"]
    assert by_instance["server-1"]["dropped"] is None
    assert "could not be read" in by_instance["server-1"]["detail"]
    document = json.loads(report_of(runs, run_id, "json").content)
    assert {note["kind"] for note in document["notes"]} == {"gap"}
    assert kinds(runs, run_id).index("gap") < kinds(runs, run_id).index("stopped")


async def test_a_clean_run_has_no_gap(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine())
    run_id = await finished_run(runner)
    assert "gap" not in kinds(runs, run_id)


def test_a_retention_policy_that_keeps_nothing_is_a_usage_error(tmp_path: Path) -> None:
    from igs_iec104_mcp.server import main

    assert main(["--project", str(tmp_path), "--keep-days", "-1"]) == 2
