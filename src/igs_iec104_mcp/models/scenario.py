"""A scenario: declarative steps run against a bench (IMPLEMENTATION.md §8).

A step names its action; an action outside the list is refused with the list of the
actions. There is no step that runs a shell command or a script. A scenario is checked
against its bench by `check_scenario`: every name resolves, a step only asks an instance
or an external target for what it can do, and a value fits the point it targets."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from igs_iec104_mcp.models.bench import TestBench
from igs_iec104_mcp.models.common import (
    Address,
    Definition,
    DefinitionError,
    GlobalTiming,
    Issue,
    Name,
    SchemaVersion,
    Timing,
)
from igs_iec104_mcp.models.points import PointKind, check_value

ACTIONS: tuple[str, ...] = (
    "wait_transfer_started",
    "wait_connection",
    "general_interrogation",
    "assert_point",
    "expect_point",
    "update_point",
    "send_command",
    "stop_instance",
    "start_instance",
    "parallel",
)
DEFAULT_TIMEOUT: str = "10s"
# The tolerance of a numeric comparison: zero, or any non-negative number.
Tolerance = Field(
    default=0.0, ge=0, description="The tolerance of a numeric comparison."
)


class WaitTransferStarted(Definition):
    action: Literal["wait_transfer_started"]
    client: Name
    timeout: Timing = DEFAULT_TIMEOUT


class WaitConnection(Definition):
    action: Literal["wait_connection"]
    instance: Name = Field(
        description="The instance or external target whose connection is awaited."
    )
    timeout: Timing = DEFAULT_TIMEOUT


class GeneralInterrogation(Definition):
    action: Literal["general_interrogation"]
    client: Name
    timeout: Timing = DEFAULT_TIMEOUT


class AssertPoint(Definition):
    """Checks the client's point image as it stands (§8)."""

    action: Literal["assert_point"]
    client: Name
    address: Address
    kind: PointKind
    value: Any
    tolerance: float = Tolerance

    @model_validator(mode="after")
    def _check_value(self) -> AssertPoint:
        check_value(self.kind, self.value, f"point {self.address}")
        return self


class ExpectPoint(Definition):
    """Waits for a new observation of the client after the step's cursor (§8)."""

    action: Literal["expect_point"]
    client: Name
    address: Address
    kind: PointKind
    value: Any
    tolerance: float = Tolerance
    timeout: Timing = DEFAULT_TIMEOUT

    @model_validator(mode="after")
    def _check_value(self) -> ExpectPoint:
        check_value(self.kind, self.value, f"point {self.address}")
        return self


class UpdatePoint(Definition):
    """Sets a point of a station. The value must fit the point's kind in the instance's
    definition."""

    action: Literal["update_point"]
    server: Name
    address: Address
    value: Any


class SendCommand(Definition):
    """Sends a single or double command. A select, then an execution, is the usual "
    "pair (§7)."""

    action: Literal["send_command"]
    target: Name = Field(
        description="A client instance, or an external target that allows the command."
    )
    address: Address
    command: Literal["single", "double"]
    value: Any = Field(
        description='true or false for a single command; "on" or "off" for a double.'
    )
    select: StrictBool = False
    timeout: Timing = DEFAULT_TIMEOUT

    @model_validator(mode="after")
    def _check_value(self) -> SendCommand:
        if self.command == "single" and not isinstance(self.value, bool):
            raise ValueError(
                f"a single command takes true or false, not {self.value!r}"
            )
        if self.command == "double" and self.value not in {"on", "off"}:
            raise ValueError(
                f'a double command takes "on" or "off", not {self.value!r}'
            )
        return self


class StopInstance(Definition):
    action: Literal["stop_instance"]
    instance: Name


class StartInstance(Definition):
    action: Literal["start_instance"]
    instance: Name


class Parallel(Definition):
    """Runs branches of steps at the same time. Each branch is a list of steps, in "
    "order."""

    action: Literal["parallel"]
    branches: list[list[Step]] = Field(min_length=1)
    fail_fast: StrictBool = True

    @model_validator(mode="after")
    def _check_branches(self) -> Parallel:
        for number, branch in enumerate(self.branches):
            if not branch:
                raise ValueError(
                    f"branch {number} of a parallel step has no steps: remove it or "
                    f"add a step"
                )
        return self


Step = Annotated[
    WaitTransferStarted
    | WaitConnection
    | GeneralInterrogation
    | AssertPoint
    | ExpectPoint
    | UpdatePoint
    | SendCommand
    | StopInstance
    | StartInstance
    | Parallel,
    Field(discriminator="action"),
]
Parallel.model_rebuild()


class Scenario(Definition):
    """The definition of a scenario against one bench."""

    schema_version: SchemaVersion
    name: Name
    bench: Name = Field(description="The name of the bench the scenario runs on.")
    steps: list[Step] = Field(min_length=1)
    cleanup: list[Step] = Field(
        default_factory=list, description="Run after success, failure, or cancellation."
    )
    global_timeout: GlobalTiming | None = Field(
        default=None, description="The longest the whole scenario may run."
    )
    fail_fast: StrictBool = True

    @field_validator("steps", "cleanup", mode="before")
    @classmethod
    def _known_actions(cls, value: Any) -> Any:
        _refuse_unknown_actions(value)
        return value


def _refuse_unknown_actions(steps: Any) -> None:
    """Raises ValueError for a step whose action is not one of ACTIONS, at any depth."""
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        action = step.get("action")
        if action not in ACTIONS:
            raise ValueError(
                f"unsupported action {action!r}: the actions are {', '.join(ACTIONS)}"
            )
        for branch in step.get("branches", []) if action == "parallel" else []:
            _refuse_unknown_actions(branch)


def check_scenario(scenario: Scenario, bench: TestBench) -> None:
    """Checks a scenario against its bench. Raises DefinitionError with every problem"
    "found."""
    issues: list[Issue] = []
    if scenario.bench != bench.name:
        issues.append(
            Issue(
                "bench",
                (
                    f"the scenario runs on {scenario.bench!r}, but the bench is "
                    f"{bench.name!r}"
                ),
            )
        )
    checker = _Checker(bench)
    for where, steps in (("steps", scenario.steps), ("cleanup", scenario.cleanup)):
        checker.steps(steps, where, issues)
    if issues:
        raise DefinitionError(issues)


class _Checker:
    """Resolves the names of a scenario's steps against its bench."""

    def __init__(self, bench: TestBench) -> None:
        self.instances = {instance.name: instance for instance in bench.instances}
        self.targets = {target.name: target for target in bench.external_targets}

    def steps(self, steps: list[Step], where: str, issues: list[Issue]) -> None:
        for index, step in enumerate(steps):
            self.step(step, f"{where}[{index}]", issues)

    def step(self, step: Step, where: str, issues: list[Issue]) -> None:
        if isinstance(step, Parallel):
            for number, branch in enumerate(step.branches):
                self.steps(branch, f"{where}.branches[{number}]", issues)
        elif isinstance(
            step, (WaitTransferStarted, GeneralInterrogation, AssertPoint, ExpectPoint)
        ):
            self.client(step.client, where, issues)
        elif isinstance(step, WaitConnection):
            self.peer(step.instance, where, issues)
        elif isinstance(step, UpdatePoint):
            self.update(step, where, issues)
        elif isinstance(step, SendCommand):
            self.command(step, where, issues)
        elif isinstance(step, (StopInstance, StartInstance)):
            self.managed(step.instance, step.action, where, issues)

    def peer(self, name: str, where: str, issues: list[Issue]) -> None:
        if name not in self.instances and name not in self.targets:
            issues.append(
                Issue(
                    where,
                    f"{name!r} is not an instance or an external target of the bench",
                )
            )

    def client(self, name: str, where: str, issues: list[Issue]) -> None:
        instance = self.instances.get(name)
        if instance is None:
            issues.append(
                Issue(
                    where,
                    f"{name!r} is not an instance of the bench; a step names a client "
                    f"instance",
                )
            )
        elif instance.role != "client":
            issues.append(
                Issue(
                    where,
                    f"{name!r} is a server, and only a client has an image to check",
                )
            )

    def managed(self, name: str, action: str, where: str, issues: list[Issue]) -> None:
        if name in self.targets:
            issues.append(
                Issue(
                    where,
                    f"{action} needs an instance; {name!r} is an external target, "
                    f"which the bench "
                    "does not manage",
                )
            )
        elif name not in self.instances:
            issues.append(Issue(where, f"{name!r} is not an instance of the bench"))

    def update(self, step: UpdatePoint, where: str, issues: list[Issue]) -> None:
        instance = self.instances.get(step.server)
        if instance is None:
            issues.append(
                Issue(where, f"{step.server!r} is not an instance of the bench")
            )
            return
        if instance.role != "server":
            issues.append(
                Issue(where, f"{step.server!r} is a client, which serves no points")
            )
            return
        point = next((p for p in instance.points if p.address == step.address), None)
        if point is None:
            issues.append(
                Issue(where, f"{step.server!r} has no point at address {step.address}")
            )
            return
        try:
            check_value(point.kind, step.value, f"point {step.address}")
        except ValueError as error:
            issues.append(Issue(where, str(error)))

    def command(self, step: SendCommand, where: str, issues: list[Issue]) -> None:
        if step.target in self.targets:
            target = self.targets[step.target]
            allowed = sorted(op.value for op in target.allowed_operations)
            if target.role != "client":
                issues.append(
                    Issue(
                        where,
                        f"{step.target!r} is a server, so it cannot send a command",
                    )
                )
            elif "command" not in allowed:
                listed = ", ".join(allowed) or "none"
                issues.append(
                    Issue(
                        where,
                        f"{step.target!r} does not allow the 'command' operation "
                        f"(allowed: {listed})",
                    )
                )
            return
        if step.target not in self.instances:
            issues.append(
                Issue(
                    where,
                    f"{step.target!r} is not an instance or an external target of the "
                    f"bench",
                )
            )
        elif self.instances[step.target].role != "client":
            issues.append(
                Issue(
                    where, f"{step.target!r} is a server, so it cannot send a command"
                )
            )
