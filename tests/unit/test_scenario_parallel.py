"""Parallel branches and barriers of the scenario engine.

Task E4 (IMPLEMENTATION.md §8).

A barrier is reached when every branch has ended. The tests cover that barrier, a branch
failure with fail-fast on and off, a deadline during a barrier, the global deadline and
a cancellation during the branches, and branches nested in branches. The steps are fakes
that log when they start and end, so the order of the branches can be checked."""

import asyncio
import time
from typing import Any

from igs_iec104_mcp.models.run import Outcome, RunStatus
from igs_iec104_mcp.models.scenario import Scenario
from igs_iec104_mcp.scenarios.engine import (
    Context,
    Event,
    Handler,
    Observation,
    Result,
    StepFailed,
    execute,
)


class Idle:
    """A source with no events: the steps here are judged by their handlers alone."""

    async def last_seq(self, instance: str) -> int:
        return 0

    async def events_after(self, instance: str, cursor: int) -> list[Event]:
        return []

    async def image(self, instance: str, address: int) -> Observation | None:
        return None


def wait(instance: str) -> dict[str, Any]:
    return {"action": "wait_connection", "instance": instance}


def parallel(*branches: list[dict[str, Any]], **changes: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "action": "parallel",
        "branches": [list(branch) for branch in branches],
    }
    step.update(changes)
    return step


def scenario(steps: list[dict[str, Any]], **changes: Any) -> Scenario:
    data: dict[str, Any] = {
        "schema_version": 1,
        "name": "parallel",
        "bench": "basic-pair",
        "steps": steps,
    }
    data.update(changes)
    return Scenario.model_validate(data)


def handlers(
    log: list[str],
    *,
    hang: frozenset[str] = frozenset(),
    fail: frozenset[str] = frozenset(),
) -> dict[str, Handler]:
    """A `wait_connection` that logs its start and its end. An instance in `hang` never
    ends, and one in `fail` does not pass."""

    async def waiting(step: Any, context: Context) -> str:
        log.append(f"start {step.instance}")
        if step.instance in fail:
            raise StepFailed(f"{step.instance} refused")
        if step.instance in hang:
            await asyncio.sleep(10)
        await asyncio.sleep(0.1)
        log.append(f"end {step.instance}")
        return str(step.instance)

    async def stopping(step: Any, context: Context) -> str:
        log.append(f"stop {step.instance}")
        return "stopped"

    return {"wait_connection": waiting, "stop_instance": stopping}


def outcomes(result: Result) -> dict[str, Outcome]:
    return {verdict.step: verdict.outcome for verdict in result.verdicts}


async def test_the_barrier_waits_for_every_branch_before_the_next_step() -> None:
    log: list[str] = []
    result = await execute(
        scenario(
            [
                parallel([wait("a1"), wait("a2")], [wait("b1")]),
                wait("c1"),
            ]
        ),
        handlers(log),
        Idle(),
    )
    # Both branches start before either ends.
    assert set(log[:2]) == {"start a1", "start b1"}
    assert log.index("end b1") < log.index("start c1")
    assert log.index("end a2") < log.index("start c1")
    assert result.status is RunStatus.PASSED
    assert outcomes(result) == {
        "step 1.1.1: wait_connection": Outcome.PASSED,
        "step 1.1.2: wait_connection": Outcome.PASSED,
        "step 1.2.1: wait_connection": Outcome.PASSED,
        "step 1: parallel": Outcome.PASSED,
        "step 2: wait_connection": Outcome.PASSED,
    }
    assert result.verdicts[-2].detail == "all 2 branches passed"
    assert (result.progress.completed, result.progress.total) == (2, 2)


async def test_a_failed_branch_step_stops_the_other_branches_under_fail_fast() -> None:
    log: list[str] = []
    started = time.monotonic()
    result = await execute(
        scenario(
            [
                parallel(
                    [wait("a1"), wait("a2")],
                    [wait("b1"), wait("b2")],
                ),
                wait("c1"),
            ]
        ),
        handlers(log, hang=frozenset({"b1"}), fail=frozenset({"a1"})),
        Idle(),
    )
    assert time.monotonic() - started < 5, "the stopped branch was not waited for"
    assert outcomes(result) == {
        "step 1.1.1: wait_connection": Outcome.FAILED,
        "step 1.2.1: wait_connection": Outcome.CANCELLED,
        "step 1: parallel": Outcome.FAILED,
    }
    assert "start b2" not in log and "start a2" not in log
    parallel_verdict = result.verdicts[-1]
    assert parallel_verdict.detail == (
        "did not pass: step 1.1.1; the other branches were stopped"
    )
    assert result.status is RunStatus.FAILED
    assert result.reason == "stopped after step 1: fail-fast"


async def test_without_fail_fast_every_branch_runs_to_its_end() -> None:
    log: list[str] = []
    result = await execute(
        scenario(
            [
                parallel(
                    [wait("a1"), wait("a2")],
                    [wait("b1"), wait("b2")],
                    fail_fast=False,
                ),
                wait("c1"),
            ]
        ),
        handlers(log, fail=frozenset({"a1"})),
        Idle(),
    )
    assert outcomes(result) == {
        "step 1.1.1: wait_connection": Outcome.FAILED,
        "step 1.1.2: wait_connection": Outcome.PASSED,
        "step 1.2.1: wait_connection": Outcome.PASSED,
        "step 1.2.2: wait_connection": Outcome.PASSED,
        "step 1: parallel": Outcome.FAILED,
    }
    assert result.verdicts[-1].detail == "did not pass: step 1.1.1"
    assert "end b2" in log and "end a2" in log
    assert result.status is RunStatus.FAILED
    assert (result.progress.completed, result.progress.total) == (1, 2)


async def test_a_deadline_during_the_barrier_ends_the_branches_still_running() -> None:
    log: list[str] = []
    started = time.monotonic()
    result = await execute(
        scenario(
            [
                parallel(
                    [wait("a1")],
                    [wait("b1"), wait("b2")],
                    timeout="300ms",
                ),
                wait("c1"),
            ]
        ),
        handlers(log, hang=frozenset({"a1", "b2"})),
        Idle(),
    )
    assert time.monotonic() - started < 5
    assert outcomes(result) == {
        "step 1.1.1: wait_connection": Outcome.TIMED_OUT,
        "step 1.2.1: wait_connection": Outcome.PASSED,
        "step 1.2.2: wait_connection": Outcome.TIMED_OUT,
        "step 1: parallel": Outcome.TIMED_OUT,
    }
    open_step = next(
        v for v in result.verdicts if v.step == "step 1.1.1: wait_connection"
    )
    assert open_step.detail == "the barrier's deadline passed during this step"
    assert result.verdicts[-1].detail == (
        "no result within 0.3 s: 2 of 2 branches still running"
    )
    assert "start c1" not in log
    assert result.status is RunStatus.FAILED


async def test_the_global_deadline_during_the_branches_records_each_open_step() -> None:
    log: list[str] = []
    result = await execute(
        scenario(
            [parallel([wait("a1")], [wait("b1")]), wait("c1")],
            global_timeout="300ms",
        ),
        handlers(log, hang=frozenset({"a1", "b1"})),
        Idle(),
    )
    assert outcomes(result) == {
        "step 1.1.1: wait_connection": Outcome.TIMED_OUT,
        "step 1.2.1: wait_connection": Outcome.TIMED_OUT,
        "step 1: parallel": Outcome.TIMED_OUT,
    }
    assert all(
        verdict.detail.endswith("during this step") for verdict in result.verdicts
    )
    assert result.status is RunStatus.FAILED
    assert result.progress.completed == 1


async def test_a_cancellation_during_the_branches_cancels_each_of_them() -> None:
    log: list[str] = []
    cancel = asyncio.Event()

    async def cancel_soon() -> None:
        await asyncio.sleep(0.1)
        cancel.set()

    canceller = asyncio.create_task(cancel_soon())
    result = await execute(
        scenario(
            [parallel([wait("a1")], [wait("b1")]), wait("c1")],
            cleanup=[{"action": "stop_instance", "instance": "a1"}],
        ),
        handlers(log, hang=frozenset({"a1", "b1"})),
        Idle(),
        cancel=cancel,
    )
    await canceller
    assert outcomes(result) == {
        "step 1.1.1: wait_connection": Outcome.CANCELLED,
        "step 1.2.1: wait_connection": Outcome.CANCELLED,
        "step 1: parallel": Outcome.CANCELLED,
        "cleanup 1: stop_instance": Outcome.PASSED,
    }
    assert result.status is RunStatus.CANCELLED
    assert result.reason == "cancelled during step 1"
    assert "start c1" not in log


async def test_a_parallel_step_inside_a_branch_has_its_own_barrier() -> None:
    log: list[str] = []
    result = await execute(
        scenario(
            [
                parallel(
                    [parallel([wait("x1")], [wait("x2")])],
                    [wait("y1")],
                ),
            ]
        ),
        handlers(log),
        Idle(),
    )
    assert outcomes(result) == {
        "step 1.1.1.1.1: wait_connection": Outcome.PASSED,
        "step 1.1.1.2.1: wait_connection": Outcome.PASSED,
        "step 1.1.1: parallel": Outcome.PASSED,
        "step 1.2.1: wait_connection": Outcome.PASSED,
        "step 1: parallel": Outcome.PASSED,
    }
    assert result.status is RunStatus.PASSED
