"""The scenario engine: the steps of a scenario in order, with an event cursor for each
step, a deadline for each step and one for the whole scenario, and fail-fast
(IMPLEMENTATION.md §8; tasks E1 and E2).

The engine knows no protocol. A step is run by the handler of its action, which the
caller supplies. A handler reads the runtimes through a `Source`: the events after a
cursor, and the point image. The engine takes the cursor before a handler starts, and a
handler only matches an event that came after its cursor, even when the source returns
older ones. An action with no handler is UNSUPPORTED, never passed. The status of the
run follows the states of §10, through a machine that refuses an impossible transition.
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
from igs_iec104_mcp.models.scenario import Scenario, Step

POLL_SECONDS = 0.05


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
    """How a run ended: its status, the verdicts of the steps that ended, its progress,
    and why it ended.
    """

    status: RunStatus
    verdicts: tuple[Verdict, ...]
    progress: Progress
    reason: str


async def execute(
    scenario: Scenario,
    handlers: Mapping[str, Handler],
    source: Source,
) -> Result:
    """Runs the steps of a scenario in order.

    Each step has its deadline, the scenario has its global deadline, and a step that
    does not pass stops the run when fail-fast is on.
    """
    machine = RunMachine()
    machine.move(RunStatus.RUNNING)
    context = Context(source)
    verdicts: list[Verdict] = []
    total = len(scenario.steps)
    reason = ""
    global_deadline = False
    pending: tuple[int, Step] | None = None
    try:
        async with asyncio.timeout(_seconds(scenario.global_timeout)):
            for index, step in enumerate(scenario.steps):
                pending = (index, step)
                outcome, detail = await _run_step(step, handlers, context)
                verdicts.append(_verdict(index, step, outcome, detail))
                pending = None
                if outcome is not Outcome.PASSED and scenario.fail_fast:
                    reason = f"stopped after step {index + 1}: fail-fast"
                    break
    except TimeoutError:
        global_deadline = True
        reason = "the scenario's global deadline passed"
        if pending is not None:
            index, step = pending
            verdicts.append(
                _verdict(index, step, Outcome.TIMED_OUT, reason + " during this step")
            )
    status = _status(verdicts, global_deadline=global_deadline)
    machine.move(status)
    return Result(
        status=status,
        verdicts=tuple(verdicts),
        progress=Progress(completed=len(verdicts), total=total),
        reason=reason or _summary(status),
    )


async def _run_step(
    step: Step, handlers: Mapping[str, Handler], context: Context
) -> tuple[Outcome, str]:
    handler = handlers.get(step.action)
    if handler is None:
        return Outcome.UNSUPPORTED, f"the engine has no handler for {step.action!r}"
    limit = _seconds(getattr(step, "timeout", None))
    context.note = None
    try:
        async with asyncio.timeout(limit):
            detail = await handler(step, context)
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


def _no_result(limit: float | None) -> str:
    if limit is None:
        return "no result before the deadline"
    return f"no result within {limit:g} s"


def _seconds(value: str | None) -> float | None:
    return None if value is None else duration_seconds(value)


def _verdict(index: int, step: Step, outcome: Outcome, detail: str) -> Verdict:
    return Verdict(
        step=f"step {index + 1}: {step.action}", outcome=outcome, detail=detail
    )


def _status(verdicts: list[Verdict], *, global_deadline: bool) -> RunStatus:
    outcomes = {verdict.outcome for verdict in verdicts}
    if outcomes & {Outcome.ERROR, Outcome.UNSUPPORTED}:
        return RunStatus.ERROR
    if global_deadline or outcomes - {Outcome.PASSED}:
        return RunStatus.FAILED
    return RunStatus.PASSED


def _summary(status: RunStatus) -> str:
    return {
        RunStatus.PASSED: "every step passed",
        RunStatus.FAILED: "a step did not pass",
        RunStatus.ERROR: "a step could not be judged",
    }.get(status, str(status))
