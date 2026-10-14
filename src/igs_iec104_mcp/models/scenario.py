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
    CommonAddress,
    Definition,
    DefinitionError,
    GlobalTiming,
    Issue,
    Name,
    SchemaVersion,
    Timing,
)
from igs_iec104_mcp.models.points import PointKind, check_value
from igs_iec104_mcp.models.target import Operation, undeclared

ACTIONS: tuple[str, ...] = (
    "wait_transfer_started",
    "wait_connection",
    "expect_refusal",
    "general_interrogation",
    "start_transfer",
    "assert_point",
    "expect_point",
    "update_point",
    "send_command",
    "stop_instance",
    "switchover",
    "inject_fault",
    "clear_fault",
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


class ExpectRefusal(Definition):
    """Expects a client's connection to be refused. The step passes when the station
    records a security alarm for the handshake, or the client records that it was
    refused, and the station accepts no connection in the meantime. A station that
    accepts the connection fails the step (§7, §8)."""

    action: Literal["expect_refusal"]
    client: Name = Field(description="The client whose connection is refused.")
    server: Name = Field(description="The station the client connects to.")
    timeout: Timing = DEFAULT_TIMEOUT


class GeneralInterrogation(Definition):
    action: Literal["general_interrogation"]
    client: Name
    timeout: Timing = DEFAULT_TIMEOUT


class QualityExpectation(Definition):
    """The quality flags an observation must have. A flag that is not named is not
    checked."""

    invalid: StrictBool | None = None
    not_topical: StrictBool | None = None
    substituted: StrictBool | None = None
    blocked: StrictBool | None = None
    overflow: StrictBool | None = None


class Expected(Definition):
    """The conditions an observation of a point must meet, besides its value (§8). A
    condition that is not named is not checked."""

    common_address: CommonAddress | None = Field(
        default=None, description="The common address the observation was sent to."
    )
    cause: int | None = Field(
        default=None, ge=1, le=63, description="The cause of transmission (1 to 63)."
    )
    stamped: StrictBool | None = Field(
        default=None, description="Whether the observation carries a time tag."
    )
    quality: QualityExpectation | None = None


class AssertPoint(Expected):
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


class ExpectPoint(Expected):
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
    timeout: Timing = DEFAULT_TIMEOUT


class InjectFault(Definition):
    """Injects a fault on a connection that goes through a fault proxy: `interrupt`
    closes its connections and refuses new ones; `latency` delays each chunk it forwards
    by `latency_ms`. An interrupt passes when the client records the loss of its
    connection (V3)."""

    action: Literal["inject_fault"]
    client: Name
    server: Name
    mode: Literal["interrupt", "latency"]
    latency_ms: int | None = Field(
        default=None,
        ge=1,
        le=60_000,
        description="The delay of the latency mode, in milliseconds.",
    )
    timeout: Timing = DEFAULT_TIMEOUT


class ClearFault(Definition):
    """Clears the fault of a connection through a fault proxy: its traffic flows again,
    and a client that was interrupted reconnects (V3)."""

    action: Literal["clear_fault"]
    client: Name
    server: Name
    timeout: Timing = DEFAULT_TIMEOUT


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


class StartTransfer(Definition):
    """Requests the data transfer of a client, and waits until it is started (§8)."""

    action: Literal["start_transfer"]
    client: Name
    timeout: Timing = DEFAULT_TIMEOUT


class StopInstance(Definition):
    """Stops a managed instance, for good: its logs are kept, and it is not started
    again. A client that was connected to it must record the loss of the link before
    the step passes: a client of one station is disconnected, and a client of a group
    records the loss of that station's endpoint (§8, §10)."""

    action: Literal["stop_instance"]
    instance: Name
    timeout: Timing = DEFAULT_TIMEOUT


class Switchover(Definition):
    """Stops the station that a client's redundancy group has started. The step passes
    when the group has switched to another connected station and started its transfer
    there (§10.5)."""

    action: Literal["switchover"]
    client: Name
    timeout: Timing = DEFAULT_TIMEOUT


class StartInstance(Definition):
    """Starts a managed instance again, on the addresses it had (§8)."""

    action: Literal["start_instance"]
    instance: Name
    timeout: Timing = DEFAULT_TIMEOUT


class Parallel(Definition):
    """Runs branches of steps at the same time. Each branch is a list of steps, in "
    "order. The step ends when every branch has ended: that is its barrier."""

    action: Literal["parallel"]
    branches: list[list[Step]] = Field(min_length=1)
    fail_fast: StrictBool = Field(
        default=True,
        description="A branch step that does not pass stops the other branches.",
    )
    timeout: Timing | None = Field(
        default=None,
        description=(
            "The longest the barrier waits for the branches. Without it, the branches "
            "are bounded by their own deadlines and by the scenario's."
        ),
    )

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
    | ExpectRefusal
    | GeneralInterrogation
    | StartTransfer
    | AssertPoint
    | ExpectPoint
    | UpdatePoint
    | SendCommand
    | StopInstance
    | Switchover
    | InjectFault
    | ClearFault
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
    check_transfers(scenario, bench, issues)
    if issues:
        raise DefinitionError(issues)


def check_transfers(scenario: Scenario, bench: TestBench, issues: list[Issue]) -> None:
    """Refuses a step that needs a client's data after another client of the same
    station has started its transfer.

    A station serves the data transfer of one connection at a time: when a connection
    starts its transfer, the other connections of the station that are not stopped are
    closed (the redundancy rule of IEC 60870-5-104 §10, as the upstream library
    implements it). The step of a closed client gets no answer and no observation, so
    it times out. The check follows the order of the steps, and it judges only what the
    bench makes certain: a client that connects to one station, on a station that no
    redundancy group connects to.
    """
    links: dict[str, list[str]] = {}
    for connection in bench.connections:
        links.setdefault(connection.client, []).append(connection.server)
    station_of = {
        client: servers[0] for client, servers in links.items() if len(servers) == 1
    }
    clients_of: dict[str, set[str]] = {}
    for client, server in station_of.items():
        clients_of.setdefault(server, set()).add(client)
    unchecked = {
        server for servers in links.values() if len(servers) > 1 for server in servers
    }
    # A parallel step runs its branches in no fixed order: its stations are not judged.
    for step in scenario.steps:
        if isinstance(step, Parallel):
            unchecked |= {
                station_of.get(name, name) for name in _named_in(step.branches)
            }

    started: set[str] = set()
    superseded: dict[str, str] = {}

    def forget(clients: set[str]) -> None:
        for client in clients:
            started.discard(client)
            superseded.pop(client, None)

    for index, step in enumerate(scenario.steps):
        where = f"steps[{index}]"
        if isinstance(step, StartTransfer):
            station = station_of.get(step.client)
            if station is None or station in unchecked:
                continue
            for other in (started & clients_of[station]) - {step.client}:
                superseded[other] = step.client
                started.discard(other)
            started.add(step.client)
            superseded.pop(step.client, None)
        elif isinstance(step, (GeneralInterrogation, ExpectPoint)):
            by = superseded.get(step.client)
            station = station_of.get(step.client)
            if by is not None and station is not None and station not in unchecked:
                issues.append(
                    Issue(
                        where,
                        f"{step.client!r} was superseded by {by!r} on {station!r}: a "
                        "station serves one client's transfer at a time, so this step "
                        f"gets no answer. Run it before {by!r} starts its transfer; "
                        f"starting {step.client!r} again closes {by!r} in turn",
                    )
                )
        elif isinstance(step, (StopInstance, StartInstance)):
            # A station that stops or starts closes its connections: no client keeps its
            # place in the order.
            forget(clients_of.get(step.instance, {step.instance}))
        elif isinstance(step, (InjectFault, ClearFault)):
            forget({step.client})


def _named_in(branches: list[list[Step]]) -> set[str]:
    """The instances and clients that the steps of the branches name, at any depth."""
    names: set[str] = set()
    for branch in branches:
        for step in branch:
            if isinstance(step, Parallel):
                names |= _named_in(step.branches)
            elif isinstance(step, (StopInstance, StartInstance)):
                names.add(step.instance)
            elif hasattr(step, "client"):
                names.add(step.client)
    return names


class _Checker:
    """Resolves the names of a scenario's steps against its bench."""

    def __init__(self, bench: TestBench) -> None:
        self.instances = {instance.name: instance for instance in bench.instances}
        self.targets = {target.name: target for target in bench.external_targets}
        self.connections = {(c.client, c.server) for c in bench.connections}
        self.faulted_connections = {
            (c.client, c.server) for c in bench.connections if c.fault_proxy
        }

    def steps(self, steps: list[Step], where: str, issues: list[Issue]) -> None:
        for index, step in enumerate(steps):
            self.step(step, f"{where}[{index}]", issues)

    def step(self, step: Step, where: str, issues: list[Issue]) -> None:
        if isinstance(step, Parallel):
            for number, branch in enumerate(step.branches):
                self.steps(branch, f"{where}.branches[{number}]", issues)
        elif isinstance(
            step,
            (
                WaitTransferStarted,
                GeneralInterrogation,
                StartTransfer,
                AssertPoint,
                ExpectPoint,
            ),
        ):
            self.client(step.client, where, issues)
        elif isinstance(step, WaitConnection):
            self.peer(step.instance, where, issues)
        elif isinstance(step, ExpectRefusal):
            self.refusal(step, where, issues)
        elif isinstance(step, UpdatePoint):
            self.update(step, where, issues)
        elif isinstance(step, SendCommand):
            self.command(step, where, issues)
        elif isinstance(step, (StopInstance, StartInstance)):
            self.managed(step.instance, step.action, where, issues)
        elif isinstance(step, Switchover):
            self.group(step.client, where, issues)
        elif isinstance(step, (InjectFault, ClearFault)):
            self.faulted(step, where, issues)

    def refusal(self, step: ExpectRefusal, where: str, issues: list[Issue]) -> None:
        self.client(step.client, where, issues)
        server = self.instances.get(step.server)
        if server is None:
            issues.append(
                Issue(where, f"{step.server!r} is not an instance of the bench")
            )
        elif server.role != "server":
            issues.append(
                Issue(
                    where,
                    f"{step.server!r} is a client: a refusal is seen by a station",
                )
            )
        elif (step.client, step.server) not in self.connections:
            issues.append(
                Issue(where, f"{step.client!r} does not connect to {step.server!r}")
            )

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

    def faulted(
        self, step: InjectFault | ClearFault, where: str, issues: list[Issue]
    ) -> None:
        if (step.client, step.server) not in self.faulted_connections:
            issues.append(
                Issue(
                    where,
                    f"{step.client!r} -> {step.server!r} is not a connection through "
                    "a fault proxy: declare it with fault_proxy: true",
                )
            )
        if isinstance(step, InjectFault):
            if step.mode == "latency" and step.latency_ms is None:
                issues.append(Issue(where, "the latency mode needs latency_ms"))
            if step.mode == "interrupt" and step.latency_ms is not None:
                issues.append(Issue(where, "latency_ms belongs to the latency mode"))

    def group(self, name: str, where: str, issues: list[Issue]) -> None:
        """A switchover is of a client that connects to a redundancy group: two or more
        stations."""
        instance = self.instances.get(name)
        if instance is None:
            issues.append(Issue(where, f"{name!r} is not an instance of the bench"))
            return
        if instance.role != "client":
            issues.append(
                Issue(where, f"{name!r} is a server: a switchover is of a client")
            )
            return
        stations = sum(1 for client, _ in self.connections if client == name)
        if stations < 2:
            issues.append(
                Issue(
                    where,
                    f"{name!r} connects to {stations} station(s): a switchover needs a "
                    "redundancy group of two or more",
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
            if target.role != "client":
                issues.append(
                    Issue(
                        where,
                        f"{step.target!r} is a server, so it cannot send a command",
                    )
                )
            elif undeclared(target, {Operation.COMMAND}):
                listed = (
                    ", ".join(sorted(op.value for op in target.allowed_operations))
                    or "none"
                )
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
