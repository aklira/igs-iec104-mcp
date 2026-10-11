"""The plan of a test bench's runtimes, worked out and checked before anything is
created (IMPLEMENTATION.md §5, §6; task D2).

The plan refuses, by name, what the runtime cannot do yet:

- a point kind the runtime does not serve (Q-002): five of the ten kinds;
- link parameters other than the runtime's defaults: the runtime takes no link flags
  (Q-003);
- a connection whose ends are not on one network, or that reaches an external target
  (external targets come with task X1);
- a client that does not have exactly one connection, since a client has one server.

Nothing here touches Docker: the plan is data, so that a bad bench is refused before any
resource exists."""

from __future__ import annotations

import json
from dataclasses import dataclass

from igs_iec104_mcp.docker.specs import (
    CONTROL_NETWORK,
    CONTROL_PORT,
    RUN_DIRECTORY,
    Limits,
    SpecError,
)
from igs_iec104_mcp.models.bench import TestBench
from igs_iec104_mcp.models.instance import Instance, LinkParameters, ResourceLimits

# The point kinds the runtime serves (its `points.rs`). The others are refused by
# name until task S4 serves them (D-019); counter is served since S4a.
SERVED_KINDS: frozenset[str] = frozenset(
    {"single", "double", "normalized", "scaled", "float", "counter"}
)
# The link parameters the runtime uses: the defaults of §9.6, which the model carries.
RUNTIME_PARAMETERS = LinkParameters()
POINTS_FILE = "points.json"
TOKEN_FILE = "control-token"
MIB = 1024 * 1024
# A relay only copies bytes: a small limit is enough for it.
RELAY_LIMITS = Limits(cpus=0.25, memory_bytes=64 * MIB, pids=32)


class PlanError(Exception):
    """The bench cannot run on the runtime. `issues` lists each problem, with its
    instance or connection."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


@dataclass(frozen=True)
class PlannedNetwork:
    name: str
    external: bool


@dataclass(frozen=True)
class PlannedRuntime:
    """A runtime of the bench: its instance, its network, its limits, and its points (a
    station's points as the runtime reads them; empty for a client)."""

    name: str
    role: str
    network: str
    common_address: int
    limits: Limits
    event_capacity: int
    points: bytes


@dataclass(frozen=True)
class BenchPlan:
    name: str
    networks: tuple[PlannedNetwork, ...]
    servers: tuple[PlannedRuntime, ...]
    clients: tuple[PlannedRuntime, ...]
    server_of: dict[str, str]


def runtime_path(name: str) -> str:
    """Where a file of the run directory is inside the container."""
    return f"{RUN_DIRECTORY}/{name}"


def server_arguments(runtime: PlannedRuntime) -> tuple[str, ...]:
    """The runtime's arguments as a station. It binds its listening port itself (port
    0),
    and reports the bound address through its readiness."""
    return (
        "--listen",
        "0.0.0.0:0",
        "--points",
        runtime_path(POINTS_FILE),
        "--common-address",
        str(runtime.common_address),
        "--control",
        f"0.0.0.0:{CONTROL_PORT}",
        "--control-token-file",
        runtime_path(TOKEN_FILE),
        "--event-capacity",
        str(runtime.event_capacity),
    )


def relay_arguments(target: str) -> tuple[str, ...]:
    """The arguments of a relay: it listens on the control port and forwards to the
    runtime's control API at `target` (`address:port`)."""
    return ("--listen", f"0.0.0.0:{CONTROL_PORT}", "--target", target)


def client_arguments(runtime: PlannedRuntime, connect: str) -> tuple[str, ...]:
    """The runtime's arguments as a client of the station at `connect`
    (`address:port`)."""
    return (
        "--connect",
        connect,
        "--common-address",
        str(runtime.common_address),
        "--control",
        f"0.0.0.0:{CONTROL_PORT}",
        "--control-token-file",
        runtime_path(TOKEN_FILE),
        "--event-capacity",
        str(runtime.event_capacity),
    )


def plan_bench(bench: TestBench) -> BenchPlan:
    """The plan of a bench. Raises PlanError with every problem, not only the first."""
    issues: list[str] = []
    if any(network.name == CONTROL_NETWORK for network in bench.networks):
        issues.append(
            f"a network is named {CONTROL_NETWORK!r}, which the controller "
            "reserves for its relays: rename it"
        )
    instances = {instance.name: instance for instance in bench.instances}
    external = {target.name for target in bench.external_targets}
    server_of: dict[str, str] = {}
    for connection in bench.connections:
        if connection.client in external or connection.server in external:
            issues.append(
                f"connection {connection.client} -> {connection.server} reaches an "
                "external target: external targets are not managed yet (task X1)"
            )
            continue
        client = instances[connection.client]
        server = instances[connection.server]
        if client.network != server.network:
            issues.append(
                f"connection {client.name} -> {server.name} is not on one network: "
                f"{client.name} is on {client.network!r}, {server.name} on "
                f"{server.network!r}"
            )
        if client.name in server_of:
            issues.append(
                f"client {client.name!r} has two connections: a client has one server"
            )
        server_of[client.name] = server.name

    servers: list[PlannedRuntime] = []
    clients: list[PlannedRuntime] = []
    for instance in bench.instances:
        issues.extend(_instance_issues(instance))
        if instance.role == "client" and instance.name not in server_of:
            issues.append(
                f"client {instance.name!r} has no connection to a server: give it one"
            )
        planned = _planned(instance, issues)
        if planned is None:
            continue
        (servers if instance.role == "server" else clients).append(planned)
    if issues:
        raise PlanError(issues)
    return BenchPlan(
        name=bench.name,
        networks=tuple(
            PlannedNetwork(name=network.name, external=network.external)
            for network in bench.networks
        ),
        servers=tuple(servers),
        clients=tuple(clients),
        server_of=server_of,
    )


def _instance_issues(instance: Instance) -> list[str]:
    issues = []
    differing = [
        name
        for name in LinkParameters.model_fields
        if getattr(instance.parameters, name) != getattr(RUNTIME_PARAMETERS, name)
    ]
    if differing:
        issues.append(
            f"instance {instance.name!r} sets link parameters "
            f"({', '.join(differing)}) that the runtime does not take yet: use the "
            "defaults until task S5 takes the flags (D-020)"
        )
    unserved = sorted({str(point.kind) for point in instance.points} - SERVED_KINDS)
    if unserved:
        issues.append(
            f"instance {instance.name!r} has point kinds the runtime does not serve "
            f"yet ({', '.join(unserved)}) until task S4 serves them (D-019)"
        )
    return issues


def _planned(instance: Instance, issues: list[str]) -> PlannedRuntime | None:
    try:
        limits = _limits(instance.limits)
    except SpecError as error:
        issues.append(f"instance {instance.name!r}: {error}")
        return None
    points = b""
    if instance.role == "server":
        points = _points_file(instance)
    return PlannedRuntime(
        name=instance.name,
        role=instance.role,
        network=instance.network,
        common_address=instance.common_address,
        limits=limits,
        event_capacity=instance.limits.event_buffer,
        points=points,
    )


def _limits(limits: ResourceLimits) -> Limits:
    return Limits(
        cpus=limits.cpu,
        memory_bytes=limits.memory_mb * MIB,
        pids=limits.pids,
    )


def _points_file(instance: Instance) -> bytes:
    """The points of a station, in the form the runtime reads (its `points.rs`)."""
    document = {
        "schema_version": 1,
        "points": [
            point.model_dump(mode="json", exclude_none=True)
            for point in instance.points
        ],
    }
    return json.dumps(document, sort_keys=True).encode("utf-8")
