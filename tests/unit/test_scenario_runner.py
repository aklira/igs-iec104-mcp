"""The runs of the scenarios against a fake engine.

Task M2 (IMPLEMENTATION.md §9 and §10). A run passes and journals each verdict before
its end; a bench that does not start or does not stop makes the run an error; a
cancelled run ends cancelled and removes its bench; a cleanup policy the run does not
carry out is refused; and a request that cannot be carried out says so with a code.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from igs_iec104_mcp.definitions import service
from igs_iec104_mcp.definitions.service import ServiceError
from igs_iec104_mcp.definitions.store import ProjectStore
from igs_iec104_mcp.models.run import TERMINAL, RunStatus
from igs_iec104_mcp.scenarios.runner import NoEngine, Runner
from igs_iec104_mcp.storage.runs import RunStore

ROOT = Path(__file__).resolve().parents[2]
BENCH = (ROOT / "examples/benches/basic-pair.yaml").read_text(encoding="utf-8")
# A scenario of the client alone: its transfer, its interrogation, and the value.
INTERROGATE = """\
schema_version: 1
name: interrogate
bench: basic-pair
steps:
  - action: wait_transfer_started
    client: client-1
    timeout: 5s
  - action: general_interrogation
    client: client-1
    timeout: 5s
  - action: assert_point
    client: client-1
    address: 100
    kind: float
    value: 12.5
    tolerance: 0.001
"""
POINT = {
    "invalid": False,
    "not_topical": False,
    "substituted": False,
    "blocked": False,
    "overflow": False,
}


class FakeClient:
    """A client runtime that holds the initial value and answers an interrogation."""

    def __init__(self, *, transfer: str) -> None:
        self.state: dict[str, Any] = {"connection": "up", "transfer": transfer}
        self.kept: list[dict[str, Any]] = []
        self.last = 0
        self.closed = False

    async def ready(self) -> dict[str, Any]:
        return dict(self.state)

    async def events(self, after: int = 0) -> dict[str, Any]:
        kept = [event for event in self.kept if event["seq"] > after]
        return {
            "events": kept,
            "first_seq": 1 if self.kept else None,
            "last_seq": self.last,
            "dropped": 0,
        }

    async def station_events(self, after: int = 0) -> dict[str, Any]:
        return {"events": [], "last_seq": 0, "dropped": 0}

    async def image_points(self) -> dict[str, Any]:
        return {
            "points": [
                {
                    "address": 100,
                    "type_id": 13,
                    "value": 12.5,
                    "quality": POINT,
                    "time": None,
                    "cause": 20,
                    "common_address": 1,
                    "observed_at_ms": 0,
                    "generation": 1,
                }
            ]
        }

    async def start_transfer(self) -> dict[str, Any]:
        self.state["transfer"] = "started"
        return {"accepted": True}

    async def general_interrogation(self) -> dict[str, Any]:
        self.last += 1
        self.kept.append({"seq": self.last, "kind": "asdu", "asdu": {"cause": 10}})
        return {"accepted": True}

    async def point(self, address: int) -> dict[str, Any]:
        return {"value": 12.5}

    async def update_point(self, address: int, value: Any) -> dict[str, Any]:
        return {"value": value}

    async def aclose(self) -> None:
        self.closed = True


class FakeHandle:
    """A started bench: its client, and whether it was removed."""

    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine
        self.client_api = FakeClient(transfer=engine.transfer)
        self.stopped = False

    def client(self, instance: str) -> FakeClient:
        return self.client_api

    async def stop(self, grace: int = 10) -> object:
        if self.engine.stop_error is not None:
            raise self.engine.stop_error
        self.stopped = True
        return None


class FakeEngine:
    """An engine that starts fake benches, or fails as the test says."""

    def __init__(
        self,
        *,
        transfer: str = "started",
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self.transfer = transfer
        self.start_error = start_error
        self.stop_error = stop_error
        self.handles: list[FakeHandle] = []

    async def check(self) -> None:
        return None

    async def start(
        self,
        bench: Any,
        run_id: str,
        evidence: Path,
        material: Any,
        await_clients: bool,
    ) -> FakeHandle:
        if self.start_error is not None:
            raise self.start_error
        handle = FakeHandle(self)
        self.handles.append(handle)
        return handle

    def identities(self) -> dict[str, str]:
        return {"runtime": "sha256:fake", "relay": "sha256:fake", "upstream": "fake"}

    async def owners(self) -> list[Any]:
        return []

    async def recover(self, owner: Any, evidence: Any) -> list[str]:
        return []


def project(tmp_path: Path, bench: str = BENCH) -> tuple[ProjectStore, RunStore]:
    store = ProjectStore(tmp_path)
    runs = RunStore(tmp_path)
    service.create(store, "bench", "basic-pair", bench)
    service.create(store, "scenario", "interrogate", INTERROGATE)
    return store, runs


async def settled(runner: Runner, run_id: str) -> Any:
    """The record of a run once it has ended."""
    async with asyncio.timeout(10):
        while True:
            record = runner.get(run_id)
            if record.status in TERMINAL:
                return record
            await asyncio.sleep(0.02)


def kinds(runs: RunStore, run_id: str) -> list[str]:
    return [event["kind"] for event in runs.events(run_id, limit=100).events]


async def test_a_run_passes_and_journals_each_verdict_before_it_ends(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    engine = FakeEngine()
    runner = Runner(store, runs, engine)
    started = await runner.start("interrogate")
    assert started.status is RunStatus.RUNNING
    record = await settled(runner, started.id)
    assert record.status is RunStatus.PASSED
    assert record.ended_at_ms is not None
    assert kinds(runs, started.id) == [
        "engine",
        "verdict",
        "verdict",
        "verdict",
        "stopped",
        "ended",
    ]
    verdicts = [e for e in runs.events(started.id).events if e["kind"] == "verdict"]
    assert [v["outcome"] for v in verdicts] == ["passed", "passed", "passed"]
    assert verdicts[0]["step"] == "step 1: wait_transfer_started"
    assert engine.handles[0].stopped
    assert engine.handles[0].client_api.closed
    assert (tmp_path / "runs" / started.id / "snapshot.json").is_file()


async def test_a_bench_that_does_not_start_makes_the_run_an_error(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine(start_error=RuntimeError("no image")))
    started = await runner.start("interrogate")
    record = await settled(runner, started.id)
    assert record.status is RunStatus.ERROR
    events = runs.events(started.id).events
    assert events[-1]["reason"] == "the bench did not start"
    error = next(event for event in events if event["kind"] == "error")
    assert "no image" in error["detail"]


async def test_a_bench_that_does_not_stop_cleanly_makes_the_run_an_error(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine(stop_error=RuntimeError("stuck")))
    started = await runner.start("interrogate")
    record = await settled(runner, started.id)
    assert record.status is RunStatus.ERROR
    assert (
        "the bench did not stop cleanly" in runs.events(started.id).events[-1]["reason"]
    )


async def test_a_cancelled_run_ends_cancelled_and_removes_its_bench(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    engine = FakeEngine(transfer="stopped")
    runner = Runner(store, runs, engine)
    started = await runner.start("interrogate")
    await asyncio.sleep(0.1)
    requested = await runner.cancel(started.id)
    assert requested.status is RunStatus.RUNNING
    record = await settled(runner, started.id)
    assert record.status is RunStatus.CANCELLED
    assert engine.handles[0].stopped
    assert "cancel_requested" in kinds(runs, started.id)


async def test_cancelling_a_run_that_has_ended_is_an_invalid_state(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine())
    started = await runner.start("interrogate")
    await settled(runner, started.id)
    with pytest.raises(ServiceError) as error:
        await runner.cancel(started.id)
    assert error.value.code == "invalid_state"


async def test_a_cleanup_policy_the_run_does_not_carry_out_is_refused(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path, BENCH + "cleanup:\n  remove: never\n")
    runner = Runner(store, runs, FakeEngine())
    with pytest.raises(ServiceError) as error:
        await runner.start("interrogate")
    assert error.value.code == "unsupported"
    assert runs.list_runs(limit=10).items == []


async def test_without_an_engine_no_run_starts(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, NoEngine())
    with pytest.raises(ServiceError) as error:
        await runner.start("interrogate")
    assert error.value.code == "engine_unavailable"
    assert "--runtime-image" in error.value.message


async def test_a_run_of_a_scenario_that_is_not_there_is_not_found(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine())
    with pytest.raises(ServiceError) as error:
        await runner.start("nothing")
    assert error.value.code == "not_found"


def test_an_unknown_run_and_a_bad_cursor_are_refused(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine())
    with pytest.raises(ServiceError) as unknown:
        runner.get("0" * 32)
    assert unknown.value.code == "not_found"
    with pytest.raises(ServiceError) as cursor:
        runner.list_runs(limit=10, cursor="not-a-number")
    assert cursor.value.code == "invalid_argument"


async def test_the_journal_is_paged_with_a_cursor(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine())
    started = await runner.start("interrogate")
    await settled(runner, started.id)
    first = runner.events(started.id, limit=2, cursor=None)
    assert len(first.events) == 2
    assert first.next_cursor is not None
    second = runner.events(started.id, limit=10, cursor=str(first.next_cursor))
    assert [event["seq"] for event in second.events] == [3, 4, 5, 6]
