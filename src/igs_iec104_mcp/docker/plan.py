"""The plan of a test bench's runtimes, worked out and checked before anything is
created (IMPLEMENTATION.md §5, §6; task D2).

The plan refuses, by name, what the runtime cannot do yet:

- a point kind the runtime does not serve (Q-002): five of the ten kinds;
- link parameters other than the runtime's defaults: the runtime takes no link flags
  (Q-003);
- a connection whose ends are not on one network, or that reaches an external target
  (external targets come with task X1);
- a client with no connection, or with more stations than a redundancy group can hold
  (eight, the runtime's limit, IEC 60870-5-104 §10).

Nothing here touches Docker: the plan is data, so that a bad bench is refused before any
resource exists."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from igs_iec104_mcp.docker.specs import (
    CONTROL_NETWORK,
    CONTROL_PORT,
    RUN_DIRECTORY,
    Limits,
    SpecError,
)
from igs_iec104_mcp.models.bench import TestBench
from igs_iec104_mcp.models.instance import (
    Instance,
    InstanceTls,
    LinkParameters,
    ResourceLimits,
)

# The point kinds the runtime serves (its `points.rs`): all ten since S4d (D-019).
# A kind the runtime does not serve is refused by name; the refusal serves new kinds.
SERVED_KINDS: frozenset[str] = frozenset(
    {
        "single",
        "double",
        "normalized",
        "scaled",
        "float",
        "counter",
        "step",
        "bitstring",
        "packed",
        "normalized_unqualified",
    }
)
POINTS_FILE = "points.json"
TOKEN_FILE = "control-token"
TLS_CERTIFICATE_FILE = "tls-certificate.der"
TLS_KEY_FILE = "tls-key.der"
TLS_TRUST_FILE = "tls-trust.der"
MIB = 1024 * 1024
# The most stations a client connects to: a redundancy group of this size (the runtime's
# limit).
MAX_STATIONS_PER_CLIENT = 8
# A relay only copies bytes: a small limit is enough for it.
RELAY_LIMITS = Limits(cpus=0.25, memory_bytes=64 * MIB, pids=32)
# The port a fault proxy listens on inside its container. The clients of a faulted
# connection are given the proxy's address and this port (V3).
PROXY_PORT = 2404


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
class PlannedPublication:
    """A station's published port: the port it listens on in the bench, the host address
    and port the engine publishes, and the endpoint an external client is given (X2)."""

    port: int
    bind: str
    host_port: int
    advertise: str


@dataclass(frozen=True)
class PlannedTls:
    """The TLS of a runtime: how many intermediates its chain has, and the name a client
    checks the station against. The files themselves are delivered with the bench's
    material (V1b), never recorded in the plan."""

    intermediates: int
    server_name: str | None


def tls_file_names(tls: PlannedTls) -> tuple[str, ...]:
    """The files a runtime with this TLS needs, delivered with the bench's material."""
    intermediates = tuple(tls_intermediate_file(i) for i in range(tls.intermediates))
    return (TLS_CERTIFICATE_FILE, TLS_KEY_FILE, TLS_TRUST_FILE, *intermediates)


def tls_intermediate_file(index: int) -> str:
    """The file of the intermediate certificate at `index` in the chain (DER)."""
    return f"tls-intermediate-{index}.der"


def tls_references(instance: InstanceTls) -> dict[str, str]:
    """The secret reference that each file of an instance's TLS is delivered from."""
    files = {
        TLS_CERTIFICATE_FILE: instance.certificate,
        TLS_KEY_FILE: instance.key,
        TLS_TRUST_FILE: instance.trust,
    }
    for index, reference in enumerate(instance.intermediates):
        files[tls_intermediate_file(index)] = reference
    return files


@dataclass(frozen=True)
class PlannedRuntime:
    """A runtime of the bench: its instance, its network, its limits, and its points.

    The points are a station's, as the runtime reads them; a client has none. A
    published station has an imposed listening port and its publication; any other
    runtime binds port 0.
    """

    name: str
    role: str
    network: str
    common_address: int
    limits: Limits
    event_capacity: int
    points: bytes
    link: LinkParameters
    publication: PlannedPublication | None = None
    tls: PlannedTls | None = None


@dataclass(frozen=True)
class PlannedProxy:
    """A fault proxy of one connection: it sits on the connection's network, listens on
    `PROXY_PORT` inside its container, and forwards to the server (V3)."""

    name: str
    client: str
    server: str
    network: str


@dataclass(frozen=True)
class BenchPlan:
    name: str
    networks: tuple[PlannedNetwork, ...]
    servers: tuple[PlannedRuntime, ...]
    clients: tuple[PlannedRuntime, ...]
    server_of: dict[str, tuple[str, ...]]
    proxies: tuple[PlannedProxy, ...] = ()
    proxy_of: dict[tuple[str, str], str] = field(default_factory=dict)


def runtime_path(name: str) -> str:
    """Where a file of the run directory is inside the container."""
    return f"{RUN_DIRECTORY}/{name}"


def link_arguments(link: LinkParameters) -> tuple[str, ...]:
    """The link parameters as the runtime's flags (D-020). Every flag is given, so the
    values a run uses are the ones in its plan, defaults included."""
    return (
        "--t0",
        str(link.t0),
        "--t1",
        str(link.t1),
        "--t2",
        str(link.t2),
        "--t3",
        str(link.t3),
        "--k",
        str(link.k),
        "--w",
        str(link.w),
    )


def tls_arguments(runtime: PlannedRuntime) -> tuple[str, ...]:
    """The runtime's TLS flags, with the files it finds in its run directory."""
    if runtime.tls is None:
        return ()
    arguments = [
        "--tls-certificate",
        runtime_path(TLS_CERTIFICATE_FILE),
        "--tls-key",
        runtime_path(TLS_KEY_FILE),
        "--tls-trust",
        runtime_path(TLS_TRUST_FILE),
    ]
    for index in range(runtime.tls.intermediates):
        arguments += ["--tls-intermediate", runtime_path(tls_intermediate_file(index))]
    if runtime.tls.server_name is not None:
        arguments += ["--tls-server-name", runtime.tls.server_name]
    return tuple(arguments)


def server_arguments(runtime: PlannedRuntime) -> tuple[str, ...]:
    """The runtime's arguments as a station.

    Without a publication it binds its listening port itself (port 0) and reports the
    bound address through its readiness. A published station listens on the port its
    publication names.
    """
    listen = (
        "0.0.0.0:0"
        if runtime.publication is None
        else (f"0.0.0.0:{runtime.publication.port}")
    )
    return (
        "--listen",
        listen,
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
        *link_arguments(runtime.link),
        *tls_arguments(runtime),
    )


def relay_arguments(target: str, port: int = CONTROL_PORT) -> tuple[str, ...]:
    """The arguments of a relay: it listens on `port`, the control port by default, and
    forwards to the runtime at `target` (`address:port`)."""
    return ("--listen", f"0.0.0.0:{port}", "--target", target)


# The fault proxy runs its own program from the relay image (docker/relay.Dockerfile).
PROXY_ENTRYPOINT: tuple[str, ...] = ("python", "-I", "/usr/local/bin/faultproxy.py")


def proxy_arguments(target: str, token_file: str) -> tuple[str, ...]:
    """The arguments of a fault proxy: it listens on `PROXY_PORT` for its client,
    forwards to the server at `target` (`address:port`), and its control API listens on
    the control port, with the token read from `token_file`."""
    return (
        "--listen",
        f"0.0.0.0:{PROXY_PORT}",
        "--target",
        target,
        "--control",
        f"0.0.0.0:{CONTROL_PORT}",
        "--control-token-file",
        token_file,
    )


def client_arguments(
    runtime: PlannedRuntime, connect: Sequence[str]
) -> tuple[str, ...]:
    """The runtime's arguments as a client of the stations at `connect`
    (`address:port`), one `--connect` for each, in the order the bench names them.
    Several stations form a redundancy group (IEC 60870-5-104 §10)."""
    connects = [part for address in connect for part in ("--connect", address)]
    return (
        *connects,
        "--common-address",
        str(runtime.common_address),
        "--control",
        f"0.0.0.0:{CONTROL_PORT}",
        "--control-token-file",
        runtime_path(TOKEN_FILE),
        "--event-capacity",
        str(runtime.event_capacity),
        *link_arguments(runtime.link),
        *tls_arguments(runtime),
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
    server_of: dict[str, list[str]] = {}
    proxies: list[PlannedProxy] = []
    proxy_of: dict[tuple[str, str], str] = {}
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
        server_of.setdefault(client.name, []).append(server.name)
        if connection.fault_proxy:
            name = f"proxy-{len(proxies) + 1}"
            proxies.append(
                PlannedProxy(
                    name=name,
                    client=client.name,
                    server=server.name,
                    network=client.network,
                )
            )
            proxy_of[(client.name, server.name)] = name
    for client_name, stations in server_of.items():
        if len(stations) > MAX_STATIONS_PER_CLIENT:
            issues.append(
                f"client {client_name!r} connects to {len(stations)} stations: a "
                f"redundancy group holds at most {MAX_STATIONS_PER_CLIENT}"
            )

    servers: list[PlannedRuntime] = []
    clients: list[PlannedRuntime] = []
    host_ports: dict[int, str] = {}
    for instance in bench.instances:
        if instance.publish is not None and instance.publish.host_port != 0:
            # Port 0 is the engine's choice, so two stations with it never collide.
            taken = host_ports.get(instance.publish.host_port)
            if taken is not None:
                issues.append(
                    f"instance {instance.name!r} publishes host port "
                    f"{instance.publish.host_port}, which {taken!r} publishes too: "
                    "give each published station its own host port"
                )
            host_ports[instance.publish.host_port] = instance.name
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
        server_of={name: tuple(stations) for name, stations in server_of.items()},
        proxies=tuple(proxies),
        proxy_of=proxy_of,
    )


def _instance_issues(instance: Instance) -> list[str]:
    issues = []
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
    publication = None
    if instance.publish is not None:
        publication = PlannedPublication(
            port=instance.publish.port,
            bind=instance.publish.bind,
            host_port=instance.publish.host_port,
            advertise=instance.publish.advertise,
        )
    tls = None
    if instance.tls is not None:
        tls = PlannedTls(
            intermediates=len(instance.tls.intermediates),
            server_name=instance.tls.server_name,
        )
    return PlannedRuntime(
        publication=publication,
        tls=tls,
        name=instance.name,
        role=instance.role,
        network=instance.network,
        common_address=instance.common_address,
        limits=limits,
        event_capacity=instance.limits.event_buffer,
        points=points,
        link=instance.parameters,
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
