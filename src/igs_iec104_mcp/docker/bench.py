"""The lifecycle of a test bench's runtimes (IMPLEMENTATION.md §6; task D2): networks,
then the stations, then the clients; readiness of each; the addresses the runtimes bind;
a graceful stop with a deadline; the evidence kept; and the rollback of a partial start.

A runtime stays on its bench network, which is internal, and publishes no port. Its
control API is reached through a relay: a small container on the control network, the
only network the host can reach, which forwards to the runtime's control port (D-018). A
client is given its station's address: the station's address on their network and the
port the station bound, both read from the engine and from the station's readiness. The
runtime takes IP addresses, not container names.

A failure at any step rolls back what the start created, and the evidence of the
containers that ran is kept before they are removed. The control token and the points
are copied into the runtimes and never written to the evidence."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from igs_iec104_mcp.docker.adapter import Creation, DockerAdapter, DockerError, Resource
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.docker.plan import (
    POINTS_FILE,
    RELAY_LIMITS,
    TOKEN_FILE,
    BenchPlan,
    PlannedRuntime,
    client_arguments,
    plan_bench,
    relay_arguments,
    server_arguments,
    tls_file_names,
)
from igs_iec104_mcp.docker.specs import (
    CONTROL_NETWORK,
    CONTROL_PORT,
    ContainerSpec,
    NetworkSpec,
    PortPublication,
    part_of,
    resource_name,
)
from igs_iec104_mcp.models.bench import TestBench
from igs_iec104_mcp.runtime.api import ControlApiError, ControlClient
from igs_iec104_mcp.runtime.process import read_control_token, write_control_token

# Called before each step of the start, with the step's name. It may raise to inject a
# failure: the tests use it to check every step's rollback.
Checkpoint = Callable[[str], Awaitable[None]]
POLL_SECONDS = 0.2
TAIL_CHARACTERS = 800


class BenchError(DockerError):
    """A runtime did not come up, or stopped before it was ready. The message names the
    runtime and says what it reported."""


@dataclass(frozen=True)
class Member:
    """A runtime of a running bench: its plan, its container, its relay, the control
    address the host reaches through the relay, and the address its station bound (None
    for a client). A published station also has the relay of its IEC port, and the
    endpoint it advertises (X2); the other fields are None for the others."""

    runtime: PlannedRuntime
    resource: Resource
    relay: Resource
    control: str
    listen: str | None
    publication: Resource | None = None
    advertised: str | None = None


@dataclass(frozen=True)
class StopReport:
    """How each runtime ended, and how many resources were removed. A runtime that
    exited with status 0 after SIGTERM stopped gracefully."""

    exit_codes: dict[str, int | None]
    removed: int

    @property
    def graceful(self) -> bool:
        return all(code == 0 for code in self.exit_codes.values())


@dataclass
class RunningBench:
    """A bench that is up: its members, its owner, its evidence directory, and the
    control token of its runtimes. The token is kept in memory and never printed; the
    evidence never holds it."""

    ownership: Ownership
    members: dict[str, Member]
    evidence: Path
    token: str = field(repr=False)
    _adapter: DockerAdapter = field(repr=False)

    def client(self, instance: str) -> ControlClient:
        """A control client for one runtime, through its relay. The caller closes it."""
        return ControlClient(self.members[instance].control, self.token)

    def advertised(self, instance: str) -> str | None:
        """The endpoint a published station advertises to external clients, as the plan
        declares it: `advertise:host_port`. None for a station that is not published."""
        return self.members[instance].advertised

    async def stop(self, grace: int = 10) -> StopReport:
        """Stops the clients, then the stations, with SIGTERM and a deadline of `grace`
        seconds. Each runtime's logs and record, and its relay's, are kept before the
        owner's resources are removed, even when a stop fails."""
        order = [m for m in self.members.values() if m.runtime.role == "client"]
        order += [m for m in self.members.values() if m.runtime.role == "server"]
        exit_codes: dict[str, int | None] = {}
        try:
            for member in order:
                name = member.runtime.name
                await self._adapter.stop_container(member.resource, grace)
                exit_codes[name] = await _exit_code(self._adapter, member.resource)
                await _preserve(self._adapter, member.resource, name, self.evidence)
                await _preserve(
                    self._adapter, member.relay, f"relay-{name}", self.evidence
                )
                if member.publication is not None:
                    await _preserve(
                        self._adapter,
                        member.publication,
                        f"publication-{name}",
                        self.evidence,
                    )
        finally:
            removed = await self._adapter.remove_owned(self.ownership)
        report = StopReport(exit_codes=exit_codes, removed=len(removed))
        _write_json(
            self.evidence / "stop.json",
            {
                "exit_codes": report.exit_codes,
                "graceful": report.graceful,
                "removed": report.removed,
            },
        )
        return report


async def start_bench(
    adapter: DockerAdapter,
    bench: TestBench,
    *,
    project: str,
    run: str,
    image: str,
    relay: str,
    evidence: Path,
    ready_timeout: float = 30.0,
    checkpoint: Checkpoint | None = None,
    material: Mapping[str, Mapping[str, bytes]] | None = None,
) -> RunningBench:
    """Starts a bench: the networks, then each station and its readiness, then each
    client,
    connected to its station. The plan is checked first, and so is the material: each
    runtime with TLS needs its files, given by instance name and file name. A failure
    rolls back what was created, after the evidence of the containers that ran is kept.

    `image` is the runtime image and `relay` the relay image, each by digest or
    identity."""
    plan = plan_bench(bench)
    material = material or {}
    missing = [
        f"{runtime.name} needs {name}"
        for runtime in (*plan.servers, *plan.clients)
        if runtime.tls is not None
        for name in tls_file_names(runtime.tls)
        if name not in material.get(runtime.name, {})
    ]
    if missing:
        raise BenchError("the TLS material is missing: " + "; ".join(missing))
    ownership = Ownership(project=project, bench=bench.name, run=run)
    evidence.mkdir(parents=True, exist_ok=True)
    steps = _Steps(checkpoint)
    await steps("image")
    image_id = await adapter.image_id(image)
    relay_id = await adapter.image_id(relay)
    _write_json(
        evidence / "manifest.json",
        {
            "bench": bench.name,
            "run": run,
            "image": image,
            "image_id": image_id,
            "relay": relay,
            "relay_id": relay_id,
            "networks": [network.name for network in plan.networks],
            "runtimes": [runtime.name for runtime in (*plan.servers, *plan.clients)],
        },
    )
    with tempfile.TemporaryDirectory(prefix="igs-mcp-") as scratch:
        token_path = write_control_token(Path(scratch))
        token = read_control_token(token_path)
        async with adapter.creating(ownership) as creation:
            try:
                members = await _bring_up(
                    adapter,
                    plan,
                    ownership,
                    creation,
                    image_id=image_id,
                    relay_id=relay_id,
                    token=token,
                    steps=steps,
                    ready_timeout=ready_timeout,
                    material=material,
                )
            except BaseException:
                await _preserve_created(adapter, creation.created, evidence)
                raise
    return RunningBench(
        ownership=ownership,
        members=members,
        evidence=evidence,
        token=token,
        _adapter=adapter,
    )


async def recover(
    adapter: DockerAdapter, owner: Ownership, evidence: Path | None
) -> list[str]:
    """Removes the resources of a bench that no controller runs any more (IMPLEMENTATION
    §10; task D3). The logs and records of its containers are kept in `evidence` first,
    when there is one. A container that cannot be read is skipped, and nothing that is
    not labelled for the owner is touched. Returns the names of what was removed."""
    if evidence is not None:
        for resource in await adapter.owned(owner):
            if resource.kind == "container":
                await _preserve(adapter, resource, part_of(resource.name), evidence)
    removed = await adapter.remove_owned(owner)
    return [resource.name for resource in removed]


class _Steps:
    def __init__(self, checkpoint: Checkpoint | None) -> None:
        self._checkpoint = checkpoint

    async def __call__(self, name: str) -> None:
        if self._checkpoint is not None:
            await self._checkpoint(name)


async def _bring_up(
    adapter: DockerAdapter,
    plan: BenchPlan,
    ownership: Ownership,
    creation: Creation,
    *,
    image_id: str,
    relay_id: str,
    token: str,
    steps: _Steps,
    ready_timeout: float,
    material: Mapping[str, Mapping[str, bytes]],
) -> dict[str, Member]:
    for network in plan.networks:
        await steps(f"network:{network.name}")
        await creation.network(NetworkSpec(network.name, internal=not network.external))
    await steps(f"network:{CONTROL_NETWORK}")
    await creation.network(NetworkSpec(CONTROL_NETWORK, internal=False))
    members: dict[str, Member] = {}
    for server in plan.servers:
        members[server.name] = await _launch(
            adapter,
            creation,
            ownership,
            image_id=image_id,
            relay_id=relay_id,
            runtime=server,
            token=token,
            steps=steps,
            ready_timeout=ready_timeout,
            connect=None,
            files=material.get(server.name, {}),
        )
    for client in plan.clients:
        station = members[plan.server_of[client.name]]
        address = await _station_address(adapter, station, ownership, client.network)
        members[client.name] = await _launch(
            adapter,
            creation,
            ownership,
            image_id=image_id,
            relay_id=relay_id,
            runtime=client,
            token=token,
            steps=steps,
            ready_timeout=ready_timeout,
            connect=address,
            files=material.get(client.name, {}),
        )
    return members


async def _launch(
    adapter: DockerAdapter,
    creation: Creation,
    ownership: Ownership,
    *,
    image_id: str,
    relay_id: str,
    runtime: PlannedRuntime,
    token: str,
    steps: _Steps,
    ready_timeout: float,
    connect: str | None,
    files: Mapping[str, bytes],
) -> Member:
    """A runtime and its relay: the runtime starts on its network, then the relay is
    created on the control network (where the engine publishes its port) and joined to
    the runtime's network, and then the runtime is reached through it."""
    name = runtime.name
    await steps(f"create:{name}")
    arguments = (
        server_arguments(runtime)
        if connect is None
        else client_arguments(runtime, connect)
    )
    resource = await creation.container(
        ContainerSpec(
            instance=name,
            image=image_id,
            network=runtime.network,
            limits=runtime.limits,
            arguments=arguments,
        )
    )
    await adapter.put_files(
        resource,
        {
            POINTS_FILE: runtime.points,
            TOKEN_FILE: token.encode("utf-8"),
            **files,
        },
    )
    await steps(f"start:{name}")
    await adapter.start_container(resource)
    ip = await _address_on(adapter, resource, ownership, runtime.network)
    await steps(f"relay:{name}")
    relay = await creation.container(
        ContainerSpec(
            instance=f"relay-{name}",
            image=relay_id,
            network=CONTROL_NETWORK,
            limits=RELAY_LIMITS,
            arguments=relay_arguments(f"{ip}:{CONTROL_PORT}"),
            publish_control=True,
        )
    )
    await adapter.connect_network(
        relay, resource_name(ownership, runtime.network), ownership
    )
    await adapter.start_container(relay)
    control = await _control_address(adapter, relay)
    publication: Resource | None = None
    advertised: str | None = None
    if runtime.publication is not None:
        publication = await _publish(
            adapter, creation, ownership, runtime, relay_id, ip, steps
        )
        advertised = f"{runtime.publication.advertise}:{runtime.publication.host_port}"
    await steps(f"ready:{name}")
    body = await _await(
        adapter, resource, control, token, ready_timeout, lambda body: body["ready"]
    )
    listen: str | None = None
    if runtime.role == "server":
        listen = str(body["listen_address"])
    else:
        await steps(f"connect:{name}")
        await _await(
            adapter,
            resource,
            control,
            token,
            ready_timeout,
            lambda body: body.get("connection") == "up",
        )
    return Member(
        runtime=runtime,
        resource=resource,
        relay=relay,
        control=control,
        listen=listen,
        publication=publication,
        advertised=advertised,
    )


async def _publish(
    adapter: DockerAdapter,
    creation: Creation,
    ownership: Ownership,
    runtime: PlannedRuntime,
    relay_id: str,
    ip: str,
    steps: _Steps,
) -> Resource:
    """The relay of a published station's IEC port: it listens on the port the station
    listens on, and the engine publishes it on the address and host port of the plan. It
    joins the station's network, as the control relay does."""
    publication = runtime.publication
    if publication is None:
        raise BenchError(f"{runtime.name} is not published")
    name = runtime.name
    await steps(f"publish:{name}")
    resource = await creation.container(
        ContainerSpec(
            instance=f"iec-{name}",
            image=relay_id,
            network=CONTROL_NETWORK,
            limits=RELAY_LIMITS,
            arguments=relay_arguments(
                f"{ip}:{publication.port}", port=publication.port
            ),
            publish=PortPublication(
                container_port=publication.port,
                bind=publication.bind,
                host_port=publication.host_port,
            ),
        )
    )
    await adapter.connect_network(
        resource, resource_name(ownership, runtime.network), ownership
    )
    await adapter.start_container(resource)
    return resource


async def _control_address(adapter: DockerAdapter, relay: Resource) -> str:
    """The host address of a relay, as the engine published it on 127.0.0.1."""
    attrs = await adapter.inspect(relay)
    bindings = (attrs.get("NetworkSettings", {}).get("Ports") or {}).get(
        f"{CONTROL_PORT}/tcp"
    )
    if not bindings:
        raise BenchError(f"{relay.name} has no published control port")
    return f"127.0.0.1:{bindings[0]['HostPort']}"


async def _address_on(
    adapter: DockerAdapter,
    resource: Resource,
    ownership: Ownership,
    network: str,
) -> str:
    """The container address of a runtime on one of the bench's networks."""
    attrs = await adapter.inspect(resource)
    networks = attrs.get("NetworkSettings", {}).get("Networks") or {}
    entry = networks.get(resource_name(ownership, network)) or {}
    address = entry.get("IPAddress")
    if not address:
        raise BenchError(f"{resource.name} has no address on {network!r}")
    return str(address)


async def _station_address(
    adapter: DockerAdapter,
    station: Member,
    ownership: Ownership,
    network: str,
) -> str:
    """The address a client connects to: the station's address on the client's network,
    and the port the station bound."""
    if station.listen is None:
        raise BenchError(f"{station.runtime.name} reports no listening address")
    address = await _address_on(adapter, station.resource, ownership, network)
    port = station.listen.rsplit(":", 1)[1]
    return f"{address}:{port}"


async def _await(
    adapter: DockerAdapter,
    resource: Resource,
    control: str,
    token: str,
    timeout: float,
    done: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    """Polls a runtime's readiness, through its relay, until `done` accepts it. Raises
    BenchError when the runtime stops, or when the time runs out, with what the runtime
    last reported."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    client = ControlClient(control, token)
    last = "no answer yet"
    try:
        while True:
            attrs = await adapter.inspect(resource)
            if not (attrs.get("State") or {}).get("Running"):
                tail = (await adapter.logs(resource))[-TAIL_CHARACTERS:]
                raise BenchError(
                    f"{resource.name} stopped before it was ready: "
                    f"{tail.decode('utf-8', 'replace').strip()}"
                )
            try:
                body = await client.ready()
                if done(body):
                    return body
                last = f"not yet: {json.dumps(body, sort_keys=True)}"
            except (httpx.HTTPError, ControlApiError) as error:
                last = str(error)
            if loop.time() >= deadline:
                raise BenchError(
                    f"{resource.name} was not ready within {timeout:g} s: {last}"
                )
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await client.aclose()


async def _preserve_created(
    adapter: DockerAdapter, created: list[Resource], evidence: Path
) -> None:
    """Keeps the logs and the record of each container that a failed start created,
    before the rollback removes them. A container that cannot be read is skipped."""
    for resource in created:
        if resource.kind == "container":
            await _preserve(adapter, resource, part_of(resource.name), evidence)


async def _preserve(
    adapter: DockerAdapter, resource: Resource, name: str, evidence: Path
) -> None:
    try:
        logs = await adapter.logs(resource)
        record = await adapter.inspect(resource)
    except DockerError:
        return
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / f"{name}.log").write_bytes(logs)
    _write_json(evidence / f"{name}.json", record)


async def _exit_code(adapter: DockerAdapter, resource: Resource) -> int | None:
    attrs = await adapter.inspect(resource)
    code = (attrs.get("State") or {}).get("ExitCode")
    return int(code) if code is not None else None


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
