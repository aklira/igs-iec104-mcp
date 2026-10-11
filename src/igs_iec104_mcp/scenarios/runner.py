"""The runs of the scenarios: a run starts a scenario on its bench, follows it, can be
cancelled, and keeps its record (IMPLEMENTATION.md §9 and §10; task M2).

A run starts its bench through an engine, runs the scenario against the bench's
control APIs, and removes the bench afterwards, whatever the outcome. Each verdict of
the scenario goes to the run's journal as soon as it is known, so a client can follow
the run before it ends. The status and the times are in the run store, and the inputs
are in the run's snapshot.

The runs execute as tasks of the server's event loop. A run still going when the server
stops keeps its status, as a run interrupted by a restart; reconciling those is the task
of §10 (D3).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from igs_iec104_mcp.definitions.service import ServiceError
from igs_iec104_mcp.definitions.store import (
    Missing,
    ProjectStore,
    StoreError,
    new_run_id,
)
from igs_iec104_mcp.docker.adapter import DockerAdapter, DockerError
from igs_iec104_mcp.docker.bench import start_bench
from igs_iec104_mcp.models.bench import TestBench
from igs_iec104_mcp.models.common import DefinitionError
from igs_iec104_mcp.models.instance import ResourceLimits
from igs_iec104_mcp.models.run import TERMINAL, RunStatus, Verdict
from igs_iec104_mcp.models.scenario import Scenario, check_scenario
from igs_iec104_mcp.runtime.source import ControlSource
from igs_iec104_mcp.scenarios.actions import actions_for
from igs_iec104_mcp.scenarios.assertions import ASSERTION_HANDLERS
from igs_iec104_mcp.scenarios.engine import Result, execute
from igs_iec104_mcp.storage.runs import (
    RUNS_DIRECTORY,
    EventPage,
    InvalidTransition,
    RunEnded,
    RunError,
    RunPage,
    RunRecord,
    RunStore,
    UnknownRun,
)

# The label of the project that owns the containers of a run.
PROJECT_LABEL = "igs-mcp"
EVIDENCE = "evidence"
NO_ENGINE = (
    "the server has no runtime image: start it with --runtime-image and --relay-image"
)


class EngineUnavailable(Exception):
    """The engine that runs the benches cannot be used. The message says why."""


class Client(Protocol):
    """The control API of a client runtime, as a run uses it."""

    async def ready(self) -> dict[str, Any]: ...

    async def events(self, after: int = 0) -> dict[str, Any]: ...

    async def image_points(self) -> dict[str, Any]: ...

    async def start_transfer(self) -> dict[str, Any]: ...

    async def general_interrogation(self) -> dict[str, Any]: ...

    async def point(self, address: int) -> dict[str, Any]: ...

    async def update_point(self, address: int, value: Any) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class BenchHandle(Protocol):
    """A started bench: the control client of an instance, and its removal."""

    def client(self, instance: str) -> Client: ...

    async def stop(self, grace: int = 10) -> object: ...


class Engine(Protocol):
    """Where the benches of the runs are started."""

    async def check(self) -> None: ...

    async def start(
        self, bench: TestBench, run_id: str, evidence: Path
    ) -> BenchHandle: ...


class DockerEngine:
    """The Docker engine of the runs. A bench starts through the adapter, with the
    runtime and relay images named when the server starts."""

    def __init__(self, *, image: str, relay: str) -> None:
        self._image = image
        self._relay = relay
        self._adapter: DockerAdapter | None = None

    async def check(self) -> None:
        """The engine answers, and both images are on it by their identity: a tag is
        refused, since a bench names its images by digest (§6)."""
        try:
            if self._adapter is None:
                self._adapter = DockerAdapter.connect()
            await self._adapter.ping()
            await self._adapter.image_id(self._image)
            await self._adapter.image_id(self._relay)
        except DockerError as error:
            self._adapter = None
            raise EngineUnavailable(str(error)) from error

    async def start(self, bench: TestBench, run_id: str, evidence: Path) -> BenchHandle:
        await self.check()
        adapter = self._adapter
        if adapter is None:
            raise EngineUnavailable("the Docker engine is not connected")
        return await start_bench(
            adapter,
            bench,
            project=PROJECT_LABEL,
            run=run_id,
            image=self._image,
            relay=self._relay,
            evidence=evidence,
        )


class NoEngine:
    """The engine of a server started without runtime images: no bench can start."""

    async def check(self) -> None:
        raise EngineUnavailable(NO_ENGINE)

    async def start(self, bench: TestBench, run_id: str, evidence: Path) -> BenchHandle:
        raise EngineUnavailable(NO_ENGINE)


class _ManagedBench:
    """A started bench as the scenario's actions see it: the control clients of its
    instances. An action that restarts an instance is refused: a restart of a managed
    instance needs a fixed station port (D2)."""

    def __init__(self, handle: BenchHandle, bench: TestBench) -> None:
        self._handle = handle
        self._bench = bench
        self._clients: dict[str, Client] = {}

    def control(self, instance: str) -> Client:
        if instance not in self._clients:
            self._clients[instance] = self._handle.client(instance)
        return self._clients[instance]

    def clients_of(self, instance: str) -> list[str]:
        return [c.client for c in self._bench.connections if c.server == instance]

    async def stop(self, instance: str) -> None:
        raise RunError(
            f"{instance} cannot be stopped on a managed bench yet: a restart needs a "
            "fixed station port (D2)"
        )

    async def start(self, instance: str) -> None:
        raise RunError(
            f"{instance} cannot be started on a managed bench yet: a restart needs a "
            "fixed station port (D2)"
        )

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()


@dataclass
class _Running:
    """A run that is going: its task, and the event that cancels it."""

    task: asyncio.Task[None]
    cancel: asyncio.Event


class RunOut(BaseModel):
    """A run as a tool reports it."""

    id: str
    bench: str
    scenario: str
    status: str
    created_at_ms: int
    ended_at_ms: int | None
    events: int
    dropped: int


class RunsOut(BaseModel):
    """A page of runs as a tool reports it."""

    items: list[RunOut]
    next_cursor: str | None


class EventsOut(BaseModel):
    """A page of journal events as a tool reports it."""

    events: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: str | None


def run_out(record: RunRecord) -> RunOut:
    return RunOut(
        id=record.id,
        bench=record.bench,
        scenario=record.scenario,
        status=str(record.status),
        created_at_ms=record.created_at_ms,
        ended_at_ms=record.ended_at_ms,
        events=record.events,
        dropped=record.dropped,
    )


class Runner:
    """Starts, follows, and cancels the runs of one project."""

    def __init__(self, store: ProjectStore, runs: RunStore, engine: Engine) -> None:
        self._store = store
        self._runs = runs
        self._engine = engine
        self._running: dict[str, _Running] = {}

    async def start(self, scenario_name: str) -> RunRecord:
        """Starts a run of a scenario. The run is returned at once, running: the bench
        starts, and the scenario runs, in a task."""
        scenario, bench = self._definitions(scenario_name)
        _refuse_unsupported(bench)
        try:
            await self._engine.check()
        except EngineUnavailable as error:
            raise ServiceError("engine_unavailable", str(error)) from error
        run_id = new_run_id()
        with _failures():
            self._runs.create(run_id, bench.name, scenario.name, _now_ms())
            self._store.snapshot_run(run_id, bench.name, scenario.name)
            self._runs.move(run_id, RunStatus.RUNNING)
        cancel = asyncio.Event()
        task = asyncio.create_task(self._execute(run_id, bench, scenario, cancel))
        self._running[run_id] = _Running(task=task, cancel=cancel)
        task.add_done_callback(lambda _: self._running.pop(run_id, None))
        return self.get(run_id)

    async def cancel(self, run_id: str) -> RunRecord:
        """Asks a running run to stop.

        It ends cancelled, once its steps have stopped and its cleanup has run. The
        record returned is the state at the request.
        """
        record = self.get(run_id)
        running = self._running.get(run_id)
        if record.status in TERMINAL or running is None:
            raise ServiceError(
                "invalid_state",
                f"the run is {record.status}: only a run going on this server can be "
                "cancelled",
            )
        running.cancel.set()
        with _failures():
            self._runs.record(run_id, "cancel_requested", {})
        return record

    def get(self, run_id: str) -> RunRecord:
        with _failures():
            return self._runs.get(run_id)

    def list_runs(self, *, limit: int, cursor: str | None) -> RunPage:
        with _failures():
            return self._runs.list_runs(limit=limit, cursor=_cursor(cursor))

    def events(self, run_id: str, *, limit: int, cursor: str | None) -> EventPage:
        with _failures():
            return self._runs.events(run_id, after=_cursor(cursor), limit=limit)

    def _definitions(self, scenario_name: str) -> tuple[Scenario, TestBench]:
        try:
            scenario = self._store.scenario(scenario_name)
            bench = self._store.bench(scenario.bench)
            check_scenario(scenario, bench)
        except Missing as error:
            raise ServiceError("not_found", str(error)) from error
        except DefinitionError as error:
            raise ServiceError("invalid_definition", _describe(error)) from error
        except StoreError as error:
            raise ServiceError("storage_error", str(error)) from error
        return scenario, bench

    async def _execute(
        self, run_id: str, bench: TestBench, scenario: Scenario, cancel: asyncio.Event
    ) -> None:
        timer = asyncio.get_running_loop().call_later(
            _limit(bench), self._limit_reached, run_id, cancel
        )
        try:
            status, reason = await self._run(run_id, bench, scenario, cancel)
        finally:
            timer.cancel()
        self._runs.record(run_id, "ended", {"status": str(status), "reason": reason})
        self._runs.move(run_id, status, ended_at_ms=_now_ms())

    async def _run(
        self, run_id: str, bench: TestBench, scenario: Scenario, cancel: asyncio.Event
    ) -> tuple[RunStatus, str]:
        evidence = self._runs.root / RUNS_DIRECTORY / run_id / EVIDENCE
        try:
            handle = await self._engine.start(bench, run_id, evidence)
        except Exception as error:
            self._runs.record(
                run_id, "error", {"detail": f"the bench did not start: {error}"}
            )
            return RunStatus.ERROR, "the bench did not start"
        managed = _ManagedBench(handle, bench)
        status, reason = RunStatus.ERROR, "the run failed"
        try:
            result = await self._scenario(run_id, bench, managed, scenario, cancel)
            status, reason = result.status, result.reason
        except Exception as error:
            self._runs.record(
                run_id,
                "error",
                {"detail": f"the run failed: {type(error).__name__}: {error}"},
            )
        finally:
            await managed.close()
            stopped = await self._stop(run_id, handle)
        if not stopped:
            return RunStatus.ERROR, "the bench did not stop cleanly"
        return status, reason

    async def _scenario(
        self,
        run_id: str,
        bench: TestBench,
        managed: _ManagedBench,
        scenario: Scenario,
        cancel: asyncio.Event,
    ) -> Result:
        clients = [i.name for i in bench.instances if i.role == "client"]
        source = ControlSource({name: managed.control(name) for name in clients})
        handlers = {**ASSERTION_HANDLERS, **actions_for(managed)}

        def observe(verdict: Verdict) -> None:
            self._runs.record(
                run_id,
                "verdict",
                {
                    "step": verdict.step,
                    "outcome": str(verdict.outcome),
                    "detail": verdict.detail,
                },
            )

        return await execute(
            scenario, handlers, source, cancel=cancel, observer=observe
        )

    async def _stop(self, run_id: str, handle: BenchHandle) -> bool:
        try:
            await handle.stop()
        except Exception as error:
            self._runs.record(
                run_id,
                "error",
                {"detail": f"the bench did not stop cleanly: {error}"},
            )
            return False
        self._runs.record(run_id, "stopped", {})
        return True

    def _limit_reached(self, run_id: str, cancel: asyncio.Event) -> None:
        self._runs.record(
            run_id,
            "error",
            {"detail": "the execution time limit passed: the run is cancelled"},
        )
        cancel.set()


def _refuse_unsupported(bench: TestBench) -> None:
    policy = bench.cleanup
    if policy.remove != "success_and_failure":
        raise ServiceError(
            "unsupported",
            f"the cleanup policy {policy.remove!r} is not supported yet: a run removes "
            "its bench after success and after failure",
        )
    if not policy.keep_evidence:
        raise ServiceError(
            "unsupported", "a run keeps its evidence: keep_evidence must be true"
        )


def _limit(bench: TestBench) -> float:
    """The shortest execution time of the bench's instances, in seconds."""
    default = ResourceLimits().execution_seconds
    return float(
        min((i.limits.execution_seconds for i in bench.instances), default=default)
    )


def _cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except ValueError as error:
        raise ServiceError(
            "invalid_argument", f"{cursor!r} is not a cursor of this server"
        ) from error
    if value < 0:
        raise ServiceError("invalid_argument", f"{cursor!r} is not a cursor")
    return value


def _describe(error: DefinitionError) -> str:
    return "; ".join(str(issue) for issue in error.issues)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@contextmanager
def _failures() -> Iterator[None]:
    """Turns the run store's failures into the service's error codes."""
    try:
        yield
    except UnknownRun as error:
        raise ServiceError("not_found", str(error)) from error
    except (InvalidTransition, RunEnded) as error:
        raise ServiceError("invalid_state", str(error)) from error
    except (RunError, StoreError) as error:
        raise ServiceError("storage_error", str(error)) from error
