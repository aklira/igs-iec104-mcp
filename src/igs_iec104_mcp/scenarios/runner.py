"""The runs of the scenarios: a run starts a scenario on its bench, follows it, can be
cancelled, and keeps its record (IMPLEMENTATION.md §9 and §10; task M2).

A run starts its bench through an engine, runs the scenario against the bench's
control APIs, and removes the bench afterwards, whatever the outcome. Each verdict of
the scenario goes to the run's journal as soon as it is known, so a client can follow
the run before it ends. The status and the times are in the run store, and the inputs
are in the run's snapshot.

The runs execute as tasks of the server's event loop. A run the store holds as unended
when the server starts was left by a controller that stopped: `reconcile` marks it
interrupted, and nothing of it is resumed. Its bench, if the engine still holds one, is
listed by `recovered` and removed by `remove_recovered` (IMPLEMENTATION.md §10, D3).
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
from igs_iec104_mcp.docker.bench import recover as recover_bench
from igs_iec104_mcp.docker.bench import start_bench
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.models.bench import TestBench
from igs_iec104_mcp.models.common import DefinitionError
from igs_iec104_mcp.models.instance import ResourceLimits
from igs_iec104_mcp.models.run import TERMINAL, RunStatus, Verdict
from igs_iec104_mcp.models.scenario import Scenario, check_scenario
from igs_iec104_mcp.reporting.reports import ReportOut, report_of
from igs_iec104_mcp.runtime.pin import UPSTREAM_REVISION
from igs_iec104_mcp.runtime.source import ControlSource
from igs_iec104_mcp.scenarios.actions import actions_for
from igs_iec104_mcp.scenarios.assertions import ASSERTION_HANDLERS
from igs_iec104_mcp.scenarios.confirmation import Confirmer, Gate
from igs_iec104_mcp.scenarios.engine import Result, execute
from igs_iec104_mcp.storage.retention import RetentionPolicy
from igs_iec104_mcp.storage.runs import (
    EVIDENCE_DIRECTORY,
    MAX_PAGE,
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
EVIDENCE = EVIDENCE_DIRECTORY
INTERRUPTED_DETAIL = (
    "the server stopped during the run: no command was resumed, and the bench is left "
    "for inspection (bench_recovered)"
)
NO_ENGINE = (
    "the server has no runtime image: start it with --runtime-image and --relay-image"
)


class EngineUnavailable(Exception):
    """The engine that runs the benches cannot be used. The message says why."""


class Client(Protocol):
    """The control API of a runtime, as a run uses it."""

    async def ready(self) -> dict[str, Any]: ...

    async def events(self, after: int = 0) -> dict[str, Any]: ...

    async def station_events(self, after: int = 0) -> dict[str, Any]: ...

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

    async def owners(self) -> list[Ownership]: ...

    async def recover(self, owner: Ownership, evidence: Path | None) -> list[str]: ...

    def identities(self) -> dict[str, str]: ...


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

    def identities(self) -> dict[str, str]:
        """The images of the benches, by their identities, and the upstream revision."""
        return {
            "runtime": self._image,
            "relay": self._relay,
            "upstream": UPSTREAM_REVISION,
        }

    async def owners(self) -> list[Ownership]:
        adapter = await self._connect()
        try:
            return await adapter.owners(PROJECT_LABEL)
        except DockerError as error:
            raise EngineUnavailable(str(error)) from error

    async def recover(self, owner: Ownership, evidence: Path | None) -> list[str]:
        adapter = await self._connect()
        try:
            return await recover_bench(adapter, owner, evidence)
        except DockerError as error:
            raise EngineUnavailable(str(error)) from error

    async def _connect(self) -> DockerAdapter:
        """The adapter, connected to an engine that answers. Recovery needs the engine,
        and no image."""
        try:
            if self._adapter is None:
                self._adapter = DockerAdapter.connect()
            await self._adapter.ping()
        except DockerError as error:
            self._adapter = None
            raise EngineUnavailable(str(error)) from error
        return self._adapter


class NoEngine:
    """The engine of a server started without runtime images: no bench can start, and
    none is listed or removed."""

    async def check(self) -> None:
        raise EngineUnavailable(NO_ENGINE)

    async def start(self, bench: TestBench, run_id: str, evidence: Path) -> BenchHandle:
        raise EngineUnavailable(NO_ENGINE)

    def identities(self) -> dict[str, str]:
        return {}

    async def owners(self) -> list[Ownership]:
        raise EngineUnavailable(NO_ENGINE)

    async def recover(self, owner: Ownership, evidence: Path | None) -> list[str]:
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


class RecoveredOut(BaseModel):
    """A bench a controller left on the engine: its run and its bench, and the status of
    the run when the store knows the run."""

    run: str
    bench: str
    status: str | None


class RecoveredPage(BaseModel):
    """The benches that no task of this server runs."""

    benches: list[RecoveredOut]


class RemovedOut(BaseModel):
    """The resources removed from a recovered bench, by name."""

    run: str
    removed: list[str]


class PruneOut(BaseModel):
    """What a prune removes, or would remove. A run whose bench is still on the
    engine is kept, so that the bench stays inspectable."""

    dry_run: bool
    policy: str
    removed: list[str]
    kept_for_bench: list[str]


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

    def __init__(
        self,
        store: ProjectStore,
        runs: RunStore,
        engine: Engine,
        retention: RetentionPolicy | None = None,
    ) -> None:
        self._store = store
        self._runs = runs
        self._engine = engine
        self._retention = retention
        self._running: dict[str, _Running] = {}

    async def start(
        self, scenario_name: str, *, confirmer: Confirmer | None = None
    ) -> RunRecord:
        """Starts a run of a scenario. The run is returned at once, running: the bench
        starts, and the scenario runs, in a task. A state-changing step to an external
        target asks `confirmer` (see scenarios.confirmation)."""
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
        task = asyncio.create_task(
            self._execute(run_id, bench, scenario, cancel, confirmer)
        )
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

    def reconcile(self) -> list[str]:
        """Marks each run the store holds as unended, and that no task of this server
        runs, as interrupted. Nothing is resumed: no command is sent again, and the
        bench is left for inspection. Returns the identities of the runs marked."""
        interrupted: list[str] = []
        with _failures():
            for record in self._runs.unended():
                if record.id in self._running:
                    continue
                if record.status is RunStatus.PENDING:
                    self._runs.move(record.id, RunStatus.RUNNING)
                self._runs.record(
                    record.id, "interrupted", {"detail": INTERRUPTED_DETAIL}
                )
                self._runs.move(record.id, RunStatus.INTERRUPTED, ended_at_ms=_now_ms())
                interrupted.append(record.id)
        return interrupted

    async def recovered(self) -> RecoveredPage:
        """The benches on the engine whose run no task of this server runs."""
        owners = await self._owners()
        return RecoveredPage(
            benches=[
                RecoveredOut(run=o.run, bench=o.bench, status=self._status_of(o.run))
                for o in owners
                if o.run not in self._running
            ]
        )

    async def remove_recovered(self, run_id: str) -> RemovedOut:
        """Removes the resources of a recovered bench. The logs of its containers are
        kept with the run's evidence, when the store knows the run. Refused for a run
        this server is running."""
        if run_id in self._running:
            raise ServiceError(
                "invalid_state",
                f"the run {run_id!r} is running on this server: cancel it first",
            )
        owner = next((o for o in await self._owners() if o.run == run_id), None)
        if owner is None:
            raise ServiceError(
                "not_found", f"no bench of the run {run_id!r} is on the engine"
            )
        try:
            removed = await self._engine.recover(owner, self._evidence_of(run_id))
        except EngineUnavailable as error:
            raise ServiceError("engine_unavailable", str(error)) from error
        return RemovedOut(run=run_id, removed=removed)

    def get(self, run_id: str) -> RunRecord:
        with _failures():
            return self._runs.get(run_id)

    async def prune(self, *, dry_run: bool = True) -> PruneOut:
        """Removes the ended runs that the retention policy selects. A run whose bench
        is still on the engine is kept. With `dry_run`, nothing is removed: the result
        says what would be. Needs the engine, to know which benches are still there.

        Raises ServiceError: `invalid_argument` when the server has no retention policy,
        and `engine_unavailable` when the engine cannot be asked.
        """
        if self._retention is None:
            raise ServiceError(
                "invalid_argument",
                "no retention policy is set: start the server with --keep-runs or "
                "--keep-days",
            )
        benches = {owner.run for owner in await self._owners()}
        selected = self._retention.select(self._all_runs(), _now_ms())
        removed = [r.id for r in selected if r.id not in benches]
        kept = [r.id for r in selected if r.id in benches]
        if not dry_run:
            with _failures():
                for run_id in removed:
                    self._runs.delete(run_id)
        return PruneOut(
            dry_run=dry_run,
            policy=self._retention.describe(),
            removed=removed,
            kept_for_bench=kept,
        )

    def _all_runs(self) -> list[RunRecord]:
        records: list[RunRecord] = []
        cursor = 0
        with _failures():
            while True:
                page = self._runs.list_runs(limit=MAX_PAGE, cursor=cursor)
                records.extend(page.items)
                if page.next_cursor is None:
                    return records
                cursor = page.next_cursor

    def report(self, run_id: str, *, format: str) -> ReportOut:
        """The report of an ended run in one format: json, junit, or summary."""
        with _failures():
            return report_of(self._runs, run_id, format)

    def list_runs(self, *, limit: int, cursor: str | None) -> RunPage:
        with _failures():
            return self._runs.list_runs(limit=limit, cursor=_cursor(cursor))

    def events(self, run_id: str, *, limit: int, cursor: str | None) -> EventPage:
        with _failures():
            return self._runs.events(run_id, after=_cursor(cursor), limit=limit)

    async def _owners(self) -> list[Ownership]:
        try:
            return await self._engine.owners()
        except EngineUnavailable as error:
            raise ServiceError("engine_unavailable", str(error)) from error

    def _status_of(self, run_id: str) -> str | None:
        try:
            return str(self._runs.get(run_id).status)
        except UnknownRun:
            return None

    def _evidence_of(self, run_id: str) -> Path | None:
        """The evidence directory of a run the store knows; None otherwise."""
        try:
            self._runs.get(run_id)
        except UnknownRun:
            return None
        return self._runs.root / RUNS_DIRECTORY / run_id / EVIDENCE

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
        self,
        run_id: str,
        bench: TestBench,
        scenario: Scenario,
        cancel: asyncio.Event,
        confirmer: Confirmer | None,
    ) -> None:
        timer = asyncio.get_running_loop().call_later(
            _limit(bench), self._limit_reached, run_id, cancel
        )
        try:
            status, reason = await self._run(run_id, bench, scenario, cancel, confirmer)
        finally:
            timer.cancel()
        self._runs.record(run_id, "ended", {"status": str(status), "reason": reason})
        self._runs.move(run_id, status, ended_at_ms=_now_ms())

    async def _run(
        self,
        run_id: str,
        bench: TestBench,
        scenario: Scenario,
        cancel: asyncio.Event,
        confirmer: Confirmer | None,
    ) -> tuple[RunStatus, str]:
        evidence = self._runs.root / RUNS_DIRECTORY / run_id / EVIDENCE
        self._runs.record(run_id, "engine", self._engine.identities())
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
            result = await self._scenario(
                run_id, bench, managed, scenario, cancel, confirmer
            )
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
        confirmer: Confirmer | None,
    ) -> Result:
        clients = [i.name for i in bench.instances if i.role == "client"]
        source = ControlSource({name: managed.control(name) for name in clients})

        def record_confirmation(fields: dict[str, Any]) -> None:
            self._runs.record(run_id, "confirmation", fields)

        gate = Gate(bench, confirmer, record_confirmation)
        handlers = {
            **ASSERTION_HANDLERS,
            **actions_for(managed),
            "send_command": gate.send_command,
        }

        def observe(verdict: Verdict) -> None:
            self._runs.record(
                run_id,
                "verdict",
                {
                    "step": verdict.step,
                    "outcome": str(verdict.outcome),
                    "detail": verdict.detail,
                    "seconds": verdict.seconds,
                },
            )

        result = await execute(
            scenario, handlers, source, cancel=cancel, observer=observe
        )
        await self._record_drops(run_id, bench, managed)
        return result

    async def _record_drops(
        self, run_id: str, bench: TestBench, managed: _ManagedBench
    ) -> None:
        """Notes each runtime whose log dropped events, and each whose count cannot be
        read: the run may lack that evidence, and its report says so (§7, §10)."""
        for instance in bench.instances:
            control = managed.control(instance.name)
            try:
                if instance.role == "client":
                    body = await control.events()
                else:
                    body = await control.station_events()
                dropped = int(body.get("dropped", 0))
            except Exception as error:
                self._runs.record(
                    run_id,
                    "gap",
                    {
                        "instance": instance.name,
                        "dropped": None,
                        "detail": "the drop count could not be read: "
                        f"{type(error).__name__}: {error}",
                    },
                )
                continue
            if dropped:
                self._runs.record(
                    run_id,
                    "gap",
                    {
                        "instance": instance.name,
                        "dropped": dropped,
                        "detail": f"the runtime's log dropped {dropped} events: some "
                        f"evidence of {instance.name} is missing",
                    },
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
