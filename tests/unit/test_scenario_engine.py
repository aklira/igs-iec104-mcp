"""The scenario engine against a fake runtime (IMPLEMENTATION.md §8; task E1): the order
of the steps, the deadlines of a step and of the scenario, the observations before a
cursor, fail-fast on and off, the actions with no handler, and the states of a run."""

import asyncio
from typing import Any

import pytest

from igs_iec104_mcp.models.run import Outcome, RunStatus
from igs_iec104_mcp.models.scenario import Scenario
from igs_iec104_mcp.scenarios import engine
from igs_iec104_mcp.scenarios.assertions import observation_of
from igs_iec104_mcp.scenarios.engine import (
    Context,
    Event,
    Handler,
    InvalidTransition,
    Observation,
    RunMachine,
    StepFailed,
    execute,
)

EXPECT = "expect_point"
ASSERT = "assert_point"


def last_of(events: list[Event], instance: str) -> int:
    return max((e.seq for e in events if e.instance == instance), default=0)


class FakeRuntime:
    """The events of the instances, numbered per instance, as a runtime numbers them."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, instance: str, kind: str, **data: Any) -> Event:
        event = Event(
            seq=last_of(self.events, instance) + 1,
            instance=instance,
            kind=kind,
            data=data,
        )
        self.events.append(event)
        return event

    async def last_seq(self, instance: str) -> int:
        return last_of(self.events, instance)

    async def events_after(self, instance: str, cursor: int) -> list[Event]:
        return [e for e in self.events if e.instance == instance and e.seq > cursor]

    async def image(self, instance: str, address: int) -> Observation | None:
        last = None
        for event in self.events:
            if event.instance == instance and event.data.get("address") == address:
                last = observation_of(event)
        return last


class CarelessRuntime(FakeRuntime):
    """A source that returns every event, old ones too: the engine must not match
    them."""

    async def events_after(self, instance: str, cursor: int) -> list[Event]:
        return [e for e in self.events if e.instance == instance]


def chain_actions(chain: Scenario) -> list[str]:
    return [step.action for step in chain.steps]


def scenario(steps: list[dict[str, Any]], **changes: Any) -> Scenario:
    data: dict[str, Any] = {
        "schema_version": 1,
        "name": "engine",
        "bench": "basic-pair",
        "steps": steps,
    }
    data.update(changes)
    return Scenario.model_validate(data)


def passing() -> Handler:
    async def handler(step: Any, context: Context) -> str:
        return f"{step.action} done"

    return handler


async def expect_value(step: Any, context: Context) -> str:
    """Waits for a point with the value of the step, after the cursor of the step."""
    cursor = await context.cursor("client-1")
    event = await context.wait_for(
        "client-1",
        cursor,
        lambda e: e.kind == "point" and e.data.get("value") == step.value,
    )
    return f"seq {event.seq}"


async def failing(step: Any, context: Context) -> str:
    raise StepFailed(f"the point is not {step.value}")


def handlers(**overrides: Handler) -> dict[str, Handler]:
    table: dict[str, Handler] = {
        "wait_transfer_started": passing(),
        "general_interrogation": passing(),
        ASSERT: passing(),
        "update_point": passing(),
        EXPECT: expect_value,
    }
    table.update(overrides)
    return table


async def test_the_steps_run_in_order_and_all_pass() -> None:
    order: list[str] = []

    def recording(name: str) -> Handler:
        async def handler(step: Any, context: Context) -> str:
            order.append(name)
            return name

        return handler

    chain = scenario(
        [
            {"action": "wait_transfer_started", "client": "client-1"},
            {"action": "general_interrogation", "client": "client-1"},
            {
                "action": "update_point",
                "server": "server-1",
                "address": 100,
                "value": 1.0,
            },
        ]
    )
    result = await execute(
        chain,
        {action: recording(action) for action in chain_actions(chain)},
        FakeRuntime(),
    )
    assert order == ["wait_transfer_started", "general_interrogation", "update_point"]
    assert result.status is RunStatus.PASSED
    assert [v.outcome for v in result.verdicts] == [Outcome.PASSED] * 3
    assert (result.progress.completed, result.progress.total) == (3, 3)


async def test_a_stale_observation_before_the_cursor_is_not_matched() -> None:
    runtime = CarelessRuntime()
    runtime.publish("client-1", "point", value=18.0)  # seq 1: before the step's cursor
    runtime.publish("client-1", "point", value=12.5)  # seq 2
    check = scenario(
        [
            {
                "action": EXPECT,
                "client": "client-1",
                "address": 100,
                "value": 18.0,
                "kind": "float",
                "timeout": "300ms",
            }
        ]
    )

    async def publish_later() -> None:
        await asyncio.sleep(0.05)
        runtime.publish("client-1", "point", value=18.0)  # seq 3: after the cursor

    publisher = asyncio.create_task(publish_later())
    result = await execute(check, handlers(), runtime)
    await publisher
    assert result.status is RunStatus.PASSED
    assert result.verdicts[0].detail == "seq 3"


async def test_a_step_without_its_event_times_out_at_its_deadline() -> None:
    check = scenario(
        [
            {
                "action": EXPECT,
                "client": "client-1",
                "address": 100,
                "value": 99.0,
                "kind": "float",
                "timeout": "200ms",
            }
        ]
    )
    result = await execute(check, handlers(), FakeRuntime())
    (verdict,) = result.verdicts
    assert verdict.outcome is Outcome.TIMED_OUT
    assert verdict.detail == "no result within 0.2 s"
    assert result.status is RunStatus.FAILED


async def test_the_global_deadline_ends_the_run_during_the_step_in_progress() -> None:
    check = scenario(
        [
            {
                "action": EXPECT,
                "client": "client-1",
                "address": 100,
                "value": 99.0,
                "kind": "float",
                "timeout": "10s",
            },
            {"action": "general_interrogation", "client": "client-1"},
        ],
        global_timeout="300ms",
    )
    result = await execute(check, handlers(), FakeRuntime())
    assert [v.outcome for v in result.verdicts] == [Outcome.TIMED_OUT]
    assert "during this step" in result.verdicts[0].detail
    assert result.status is RunStatus.FAILED
    assert result.progress.completed == 1


async def test_fail_fast_stops_at_the_first_step_that_does_not_pass() -> None:
    chain = scenario(
        [
            {
                "action": ASSERT,
                "client": "client-1",
                "address": 100,
                "value": 1.0,
                "kind": "float",
            },
            {"action": "general_interrogation", "client": "client-1"},
        ]
    )
    result = await execute(chain, handlers(**{ASSERT: failing}), FakeRuntime())
    assert [v.outcome for v in result.verdicts] == [Outcome.FAILED]
    assert result.status is RunStatus.FAILED
    assert "fail-fast" in result.reason
    assert result.progress.completed == 1


async def test_without_fail_fast_the_later_steps_still_run() -> None:
    chain = scenario(
        [
            {
                "action": ASSERT,
                "client": "client-1",
                "address": 100,
                "value": 1.0,
                "kind": "float",
            },
            {"action": "general_interrogation", "client": "client-1"},
        ],
        fail_fast=False,
    )
    result = await execute(chain, handlers(**{ASSERT: failing}), FakeRuntime())
    assert [v.outcome for v in result.verdicts] == [Outcome.FAILED, Outcome.PASSED]
    assert result.status is RunStatus.FAILED
    assert result.progress.completed == 2


async def test_an_action_with_no_handler_is_unsupported_and_not_passed() -> None:
    chain = scenario(
        [
            {
                "action": "send_command",
                "target": "client-1",
                "address": 1,
                "command": "single",
                "value": True,
                "select": True,
            }
        ]
    )
    result = await execute(chain, handlers(), FakeRuntime())
    (verdict,) = result.verdicts
    assert verdict.outcome is Outcome.UNSUPPORTED
    assert result.status is RunStatus.ERROR


async def test_an_unexpected_error_of_a_handler_is_an_error_verdict() -> None:
    async def broken(step: Any, context: Context) -> str:
        raise RuntimeError("the runtime answered nothing usable")

    chain = scenario([{"action": "general_interrogation", "client": "client-1"}])
    result = await execute(chain, handlers(general_interrogation=broken), FakeRuntime())
    (verdict,) = result.verdicts
    assert verdict.outcome is Outcome.ERROR
    assert "RuntimeError" in verdict.detail
    assert result.status is RunStatus.ERROR


async def test_a_run_moves_only_along_its_states() -> None:
    machine = RunMachine()
    assert machine.status is RunStatus.PENDING
    with pytest.raises(InvalidTransition):
        machine.move(RunStatus.PASSED)
    machine.move(RunStatus.RUNNING)
    machine.move(RunStatus.PASSED)
    with pytest.raises(InvalidTransition):
        machine.move(RunStatus.RUNNING)


async def test_a_step_without_a_deadline_is_bounded_by_the_scenario_only() -> None:
    async def slow(step: Any, context: Context) -> str:
        await asyncio.sleep(0.1)
        return "slow but in time"

    chain = scenario([{"action": "start_instance", "instance": "server-1"}])
    result = await execute(chain, {"start_instance": slow}, FakeRuntime())
    assert result.status is RunStatus.PASSED
    assert result.verdicts[0].detail == "slow but in time"


# Cancellation and cleanup (E3).

STOP = {"action": "stop_instance", "instance": "server-1"}
START = {"action": "start_instance", "instance": "server-1"}
GENERAL = {"action": "general_interrogation", "client": "client-1"}
UPDATE = {"action": "update_point", "server": "server-1", "address": 100, "value": 1.0}


def logging_handlers(log: list[str], **overrides: Handler) -> dict[str, Handler]:
    """Handlers that log the action they run and pass, so that the order of the steps
    and of the cleanup can be checked."""

    def recording(action: str) -> Handler:
        async def handler(step: Any, context: Context) -> str:
            log.append(action)
            return f"{action} done"

        return handler

    table: dict[str, Handler] = {
        action: recording(action)
        for action in (
            "general_interrogation",
            "update_point",
            "stop_instance",
            "start_instance",
        )
    }
    table.update(overrides)
    return table


async def hanging(step: Any, context: Context) -> str:
    await asyncio.sleep(10)
    return "never"


async def refusing(step: Any, context: Context) -> str:
    raise StepFailed("the station refused the interrogation")


async def test_a_cancelled_step_is_stopped_and_its_request_is_closed() -> None:
    open_requests: set[str] = set()
    started = asyncio.Event()
    cancel = asyncio.Event()

    async def request_then_wait(step: Any, context: Context) -> str:
        open_requests.add("request-1")
        try:
            started.set()
            return await hanging(step, context)
        finally:
            open_requests.discard("request-1")

    async def cancel_once_started() -> None:
        await started.wait()
        cancel.set()

    log: list[str] = []
    canceller = asyncio.create_task(cancel_once_started())
    result = await execute(
        scenario([GENERAL, UPDATE]),
        logging_handlers(log, general_interrogation=request_then_wait),
        FakeRuntime(),
        cancel=cancel,
    )
    await canceller
    assert result.status is RunStatus.CANCELLED
    assert [v.outcome for v in result.verdicts] == [Outcome.CANCELLED]
    assert result.verdicts[0].detail == "cancelled during this step"
    assert result.reason == "cancelled during step 1"
    assert (result.progress.completed, result.progress.total) == (1, 2)
    assert open_requests == set(), "the handler's finally ran before the run ended"
    assert log == [], "the step after the cancelled one did not run"


async def test_a_cancel_between_steps_stops_before_the_next_step() -> None:
    cancel = asyncio.Event()
    log: list[str] = []

    async def first(step: Any, context: Context) -> str:
        log.append("first")
        cancel.set()
        return "first done"

    result = await execute(
        scenario([GENERAL, UPDATE]),
        logging_handlers(log, general_interrogation=first),
        FakeRuntime(),
        cancel=cancel,
    )
    assert log == ["first"]
    assert [v.outcome for v in result.verdicts] == [Outcome.PASSED]
    assert result.status is RunStatus.CANCELLED
    assert result.reason == "cancelled before step 2"
    assert (result.progress.completed, result.progress.total) == (1, 2)


async def test_a_run_cancelled_before_it_starts_runs_only_its_cleanup() -> None:
    cancel = asyncio.Event()
    cancel.set()
    log: list[str] = []
    result = await execute(
        scenario([GENERAL], cleanup=[STOP]),
        logging_handlers(log),
        FakeRuntime(),
        cancel=cancel,
    )
    assert log == ["stop_instance"]
    assert [v.step for v in result.verdicts] == ["cleanup 1: stop_instance"]
    assert result.status is RunStatus.CANCELLED
    assert result.reason == "cancelled before step 1"
    assert (result.progress.completed, result.progress.total) == (0, 1)


@pytest.mark.parametrize(
    "first,global_timeout,expected_status,expected_outcome",
    [
        (passing(), None, RunStatus.PASSED, Outcome.PASSED),
        (refusing, None, RunStatus.FAILED, Outcome.FAILED),
        (hanging, "200ms", RunStatus.FAILED, Outcome.TIMED_OUT),
    ],
    ids=["success", "failure", "global-deadline"],
)
async def test_cleanup_runs_after_success_failure_and_the_global_deadline(
    first: Handler,
    global_timeout: str | None,
    expected_status: RunStatus,
    expected_outcome: Outcome,
) -> None:
    log: list[str] = []
    result = await execute(
        scenario([GENERAL], cleanup=[STOP], global_timeout=global_timeout),
        logging_handlers(log, general_interrogation=first),
        FakeRuntime(),
    )
    assert log == ["stop_instance"]
    assert result.status is expected_status
    assert [v.outcome for v in result.verdicts] == [expected_outcome, Outcome.PASSED]
    assert result.verdicts[-1].step == "cleanup 1: stop_instance"


async def test_every_cleanup_step_runs_even_with_fail_fast() -> None:
    log: list[str] = []
    result = await execute(
        scenario([GENERAL, UPDATE], cleanup=[STOP, START]),
        logging_handlers(log, general_interrogation=refusing),
        FakeRuntime(),
    )
    assert log == ["stop_instance", "start_instance"]
    assert [v.outcome for v in result.verdicts] == [
        Outcome.FAILED,
        Outcome.PASSED,
        Outcome.PASSED,
    ]
    assert result.reason == "stopped after step 1: fail-fast"
    assert result.status is RunStatus.FAILED
    assert result.progress.completed == 1


async def test_a_failed_cleanup_step_fails_a_run_that_passed() -> None:
    result = await execute(
        scenario([GENERAL], cleanup=[STOP]),
        logging_handlers([], stop_instance=refusing),
        FakeRuntime(),
    )
    assert [v.outcome for v in result.verdicts] == [Outcome.PASSED, Outcome.FAILED]
    assert result.status is RunStatus.FAILED
    assert result.progress.completed == 1


async def test_an_unsupported_cleanup_step_is_an_error() -> None:
    result = await execute(
        scenario(
            [GENERAL],
            cleanup=[
                {
                    "action": "send_command",
                    "target": "client-1",
                    "address": 1,
                    "command": "single",
                    "value": True,
                    "select": True,
                }
            ],
        ),
        logging_handlers([]),
        FakeRuntime(),
    )
    assert [v.outcome for v in result.verdicts] == [Outcome.PASSED, Outcome.UNSUPPORTED]
    assert result.status is RunStatus.ERROR


async def test_a_cleanup_step_is_bounded_by_its_deadline_after_the_global_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "CLEANUP_SECONDS", 0.1)
    result = await asyncio.wait_for(
        execute(
            scenario([GENERAL], cleanup=[STOP], global_timeout="200ms"),
            logging_handlers([], general_interrogation=hanging, stop_instance=hanging),
            FakeRuntime(),
        ),
        5,
    )
    assert [v.outcome for v in result.verdicts] == [
        Outcome.TIMED_OUT,
        Outcome.TIMED_OUT,
    ]
    assert result.verdicts[1].detail == "no result within 0.1 s"
    assert result.status is RunStatus.FAILED
