"""The scenario engine: the steps of a scenario in order, with an event cursor for each
step, a deadline for each step and one for the whole scenario, fail-fast, cancellation,
and the cleanup steps (IMPLEMENTATION.md §8 and §10; tasks E1, E2, and E3).

The engine knows no protocol. A step is run by the handler of its action, which the
caller supplies. A handler reads the runtimes through a `Source`: the events after a
cursor, and the point image. The engine takes the cursor before a handler starts, and a
handler only matches an event that came after its cursor, even when the source returns
older ones. An action with no handler is UNSUPPORTED, never passed. The status of the
run follows the states of §10, through a machine that refuses an impossible transition.

A run is cancelled by setting the `cancel` event the caller holds. The step in progress
then ends CANCELLED: its handler is cancelled and awaited, so that its `finally` blocks
run before the run ends, and the steps after it do not start. The cleanup steps run
after success, failure, cancellation, and the global deadline, in order, whatever the
fail-fast setting. A cleanup step cannot be cancelled, and it always has a deadline: its
own, or `CLEANUP_SECONDS` when it has none.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from igs_iec104_mcp.models.common import duration_seconds
from igs_iec104_mcp.models.run import (
    TERMINAL,
    Outcome,
    Progress,
    RunStatus,
    Verdict,
)
from igs_iec104_mcp.models.scenario import DEFAULT_TIMEOUT, Scenario, Step

POLL_SECONDS = 0.05
# The deadline of a cleanup step that names none: the default step timeout (D-027).
CLEANUP_SECONDS = duration_seconds(DEFAULT_TIMEOUT)


@dataclass(frozen=True)
class Event:
    """An event of a runtime: its sequence number within the runtime, the instance that
    sent it, its kind, and its fields."""

    seq: int
    instance: str
    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Observation:
    """A value a client received: its sequence number, its address, the type it came in,
    its cause, its common address, its quality flags, and its time tag when it has one.
    """

    seq: int
    address: int
    type_id: int
    value: Any
    cause: int
    common_address: int
    quality: Mapping[str, bool]
    time: str | None = None


class Source(Protocol):
    """Where the events and the point images of the runtimes come from."""

    async def last_seq(self, instance: str) -> int:
        """The sequence number of the latest event of an instance, or 0 when none."""

    async def events_after(self, instance: str, cursor: int) -> list[Event]:
        """The events of an instance after the cursor, in order.

        The source may return more: the engine checks each sequence number again.
        """

    async def image(self, instance: str, address: int) -> Observation | None:
        """The last value an instance received at an address, as its point image holds
        it, or None when it has none.
        """


class StepFailed(Exception):
    """A check of a step did not hold: its verdict is FAILED."""


class Context:
    """What a handler may use: the cursor of its instance, a wait for a matching event
    after it, and the point image.

    A handler may leave a note about its last check; the engine adds the note to the
    verdict when the step's deadline passes first.
    """

    def __init__(self, source: Source) -> None:
        self._source = source
        self.note: str | None = None

    async def cursor(self, instance: str) -> int:
        return await self._source.last_seq(instance)

    async def image(self, instance: str, address: int) -> Observation | None:
        return await self._source.image(instance, address)

    async def wait_for(
        self,
        instance: str,
        cursor: int,
        match: Callable[[Event], bool],
    ) -> Event:
        """The first event of the instance after the cursor that matches.

        An event at or before the cursor is never matched. Waits until the step's
        deadline.
        """
        while True:
            for event in await self._source.events_after(instance, cursor):
                if event.seq > cursor and event.instance == instance and match(event):
                    return event
            await asyncio.sleep(POLL_SECONDS)


Handler = Callable[[Step, Context], Awaitable[str]]


class InvalidTransition(Exception):
    """A run cannot move to that status from its current one."""


class _Cancelled(Exception):
    """The run was cancelled during a step."""


TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING}),
    RunStatus.RUNNING: frozenset(TERMINAL),
}


class RunMachine:
    """The status of a run: pending, then running, then one of the ended statuses."""

    def __init__(self) -> None:
        self.status = RunStatus.PENDING

    def move(self, target: RunStatus) -> None:
        if target not in TRANSITIONS.get(self.status, frozenset()):
            raise InvalidTransition(f"a run that is {self.status} cannot be {target}")
        self.status = target


@dataclass(frozen=True)
class Result:
    """How a run ended: its status, the verdicts of its steps and cleanup steps that
    ended, its progress, and why it ended.
    """

    status: RunStatus
    verdicts: tuple[Verdict, ...]
    progress: Progress
    reason: str


@dataclass
class _Ledger:
    """What a run has done so far: its verdicts, the step in progress, and its end."""

    verdicts: list[Verdict] = field(default_factory=list)
    completed: int = 0
    current: tuple[int, Step] | None = None
    reason: str = ""
    global_deadline: bool = False
    cancelled: bool = False


async def execute(
    scenario: Scenario,
    handlers: Mapping[str, Handler],
    source: Source,
    *,
    cancel: asyncio.Event | None = None,
) -> Result:
    """Runs the steps of a scenario in order, then its cleanup steps.

    Each step has its deadline, the scenario has its global deadline, and a step that
    does not pass stops the run when fail-fast is on. Setting the `cancel` event stops
    the run: the step in progress ends CANCELLED, and the steps after it do not run.
    """
    machine = RunMachine()
    machine.move(RunStatus.RUNNING)
    context = Context(source)
    ledger = _Ledger()
    try:
        async with asyncio.timeout(_seconds(scenario.global_timeout)):
            await _steps(scenario, handlers, context, cancel, ledger)
    except TimeoutError:
        ledger.global_deadline = True
        ledger.reason = "the scenario's global deadline passed"
        if ledger.current is not None:
            index, step = ledger.current
            ledger.current = None
            ledger.completed += 1
            ledger.verdicts.append(
                _verdict(
                    f"step {index + 1}",
                    step,
                    Outcome.TIMED_OUT,
                    f"{ledger.reason} during this step",
                )
            )
    await _cleanup(scenario, handlers, context, ledger)
    status = _status(
        ledger.verdicts,
        global_deadline=ledger.global_deadline,
        cancelled=ledger.cancelled,
    )
    machine.move(status)
    return Result(
        status=status,
        verdicts=tuple(ledger.verdicts),
        progress=Progress(completed=ledger.completed, total=len(scenario.steps)),
        reason=ledger.reason or _summary(status),
    )


async def _steps(
    scenario: Scenario,
    handlers: Mapping[str, Handler],
    context: Context,
    cancel: asyncio.Event | None,
    ledger: _Ledger,
) -> None:
    """Runs the steps in order, until one of them ends the run."""
    for index, step in enumerate(scenario.steps):
        if cancel is not None and cancel.is_set():
            ledger.cancelled = True
            ledger.reason = f"cancelled before step {index + 1}"
            return
        ledger.current = (index, step)
        outcome, detail = await _run_step(step, handlers, context, cancel)
        ledger.current = None
        ledger.completed += 1
        ledger.verdicts.append(_verdict(f"step {index + 1}", step, outcome, detail))
        if outcome is Outcome.CANCELLED:
            ledger.cancelled = True
            ledger.reason = f"cancelled during step {index + 1}"
            return
        if outcome is not Outcome.PASSED and scenario.fail_fast:
            ledger.reason = f"stopped after step {index + 1}: fail-fast"
            return


async def _cleanup(
    scenario: Scenario,
    handlers: Mapping[str, Handler],
    context: Context,
    ledger: _Ledger,
) -> None:
    """Runs every cleanup step, in order, whatever the run did so far. A cleanup step is
    not cancelled, and it has a deadline of its own."""
    for index, step in enumerate(scenario.cleanup):
        outcome, detail = await _run_step(
            step, handlers, context, None, default_limit=CLEANUP_SECONDS
        )
        ledger.verdicts.append(_verdict(f"cleanup {index + 1}", step, outcome, detail))


async def _run_step(
    step: Step,
    handlers: Mapping[str, Handler],
    context: Context,
    cancel: asyncio.Event | None,
    *,
    default_limit: float | None = None,
) -> tuple[Outcome, str]:
    handler = handlers.get(step.action)
    if handler is None:
        return Outcome.UNSUPPORTED, f"the engine has no handler for {step.action!r}"
    limit = _seconds(getattr(step, "timeout", None))
    if limit is None:
        limit = default_limit
    context.note = None
    try:
        async with asyncio.timeout(limit):
            detail = await _until_cancelled(handler(step, context), cancel)
    except _Cancelled:
        return Outcome.CANCELLED, "cancelled during this step"
    except StepFailed as error:
        return Outcome.FAILED, str(error)
    except TimeoutError:
        detail = _no_result(limit)
        if context.note is not None:
            detail = f"{detail}: {context.note}"
        return Outcome.TIMED_OUT, detail
    except Exception as error:
        return Outcome.ERROR, f"{type(error).__name__}: {error}"
    return Outcome.PASSED, detail


async def _until_cancelled(
    awaitable: Awaitable[str], cancel: asyncio.Event | None
) -> str:
    """Awaits the handler of a step, unless the run is cancelled first.

    A handler that is cancelled is awaited until it ends, so that its `finally` blocks
    run before the step does.
    """
    if cancel is None:
        return await awaitable
    work: asyncio.Future[str] = asyncio.ensure_future(awaitable)
    stop: asyncio.Future[Any] = asyncio.ensure_future(cancel.wait())
    try:
        await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        if not work.done():
            raise _Cancelled
        return work.result()
    finally:
        stop.cancel()
        if not work.done():
            work.cancel()
            await asyncio.wait({work})
        if work.done() and not work.cancelled():
            work.exception()


def _no_result(limit: float | None) -> str:
    if limit is None:
        return "no result before the deadline"
    return f"no result within {limit:g} s"


def _seconds(value: str | None) -> float | None:
    return None if value is None else duration_seconds(value)


def _verdict(where: str, step: Step, outcome: Outcome, detail: str) -> Verdict:
    return Verdict(step=f"{where}: {step.action}", outcome=outcome, detail=detail)


def _status(
    verdicts: list[Verdict], *, global_deadline: bool, cancelled: bool
) -> RunStatus:
    outcomes = {verdict.outcome for verdict in verdicts}
    if outcomes & {Outcome.ERROR, Outcome.UNSUPPORTED}:
        return RunStatus.ERROR
    if cancelled:
        return RunStatus.CANCELLED
    if global_deadline or outcomes - {Outcome.PASSED}:
        return RunStatus.FAILED
    return RunStatus.PASSED


def _summary(status: RunStatus) -> str:
    return {
        RunStatus.PASSED: "every step passed",
        RunStatus.FAILED: "a step did not pass",
        RunStatus.CANCELLED: "the run was cancelled",
        RunStatus.ERROR: "a step could not be judged",
    }.get(status, str(status))
