"""The lifecycle of a test bench's runtimes (IMPLEMENTATION.md §6; task D2): networks,
then the stations, then the clients; readiness of each; the addresses the runtimes bind;
a graceful stop with a deadline; the evidence kept; and the rollback of a partial start.

The controller reaches each runtime's control API through the port the engine publishes
on
127.0.0.1. A client is given its station's address, the station's container address on
their network and the port the station bound, both read from the engine and the
station's
readiness: the runtime takes IP addresses, not container names.

A failure at any step rolls back what the start created, and the evidence of the
containers that ran is kept before they are removed. The control token and the points
are copied into the containers and never written to the evidence."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from igs_iec104_mcp.docker.adapter import Creation, DockerAdapter, DockerError, Resource
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.docker.plan import (
    POINTS_FILE,
    TOKEN_FILE,
    BenchPlan,
    PlannedRuntime,
    client_arguments,
    plan_bench,
    server_arguments,
)
from igs_iec104_mcp.docker.specs import (
    CONTROL_PORT,
    ContainerSpec,
    NetworkSpec,
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
    """A runtime of a running bench: its plan, its container, its control address on the
    host, and the address its station bound (None for a client)."""

    runtime: PlannedRuntime
    resource: Resource
    control: str
    listen: str | None


@dataclass(frozen=True)
class StopReport:
    """How each runtime ended, and how many resources were removed. A runtime that
    exited
    with status 0 after SIGTERM stopped gracefully."""

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
        """A control client for one runtime. The caller closes it."""
        return ControlClient(self.members[instance].control, self.token)

    async def stop(self, grace: int = 10) -> StopReport:
        """Stops the clients, then the stations, with SIGTERM and a deadline of `grace`
        seconds. Each runtime's logs and record are kept before its resources are
        removed.
        The owner's resources are removed even when a stop fails."""
        order = [m for m in self.members.values() if m.runtime.role == "client"]
        order += [m for m in self.members.values() if m.runtime.role == "server"]
        exit_codes: dict[str, int | None] = {}
        try:
            for member in order:
                await self._adapter.stop_container(member.resource, grace)
                exit_codes[member.runtime.name] = await _exit_code(
                    self._adapter, member.resource
                )
                await _preserve(
                    self._adapter, member.resource, member.runtime.name, self.evidence
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
    evidence: Path,
    ready_timeout: float = 30.0,
    checkpoint: Checkpoint | None = None,
) -> RunningBench:
    """Starts a bench: the networks, then each station and its readiness, then each
    client, connected to its station. The plan is checked first. A failure rolls back
    what was created, after the evidence of the runtimes that ran is kept."""
    plan = plan_bench(bench)
    ownership = Ownership(project=project, bench=bench.name, run=run)
    evidence.mkdir(parents=True, exist_ok=True)
    steps = _Steps(checkpoint)
    await steps("image")
    image_id = await adapter.image_id(image)
    evidence.mkdir(parents=True, exist_ok=True)
    _write_json(
        evidence / "manifest.json",
        {
            "bench": bench.name,
            "run": run,
            "image": image,
            "image_id": image_id,
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
                    token=token,
                    steps=steps,
                    ready_timeout=ready_timeout,
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
    token: str,
    steps: _Steps,
    ready_timeout: float,
) -> dict[str, Member]:
    for network in plan.networks:
        await steps(f"network:{network.name}")
        await creation.network(NetworkSpec(network.name, internal=not network.external))
    members: dict[str, Member] = {}
    for server in plan.servers:
        members[server.name] = await _launch(
            adapter,
            creation,
            image_id=image_id,
            runtime=server,
            token=token,
            steps=steps,
            ready_timeout=ready_timeout,
            connect=None,
        )
    for client in plan.clients:
        station = members[plan.server_of[client.name]]
        address = await _station_address(adapter, station, ownership, client.network)
        members[client.name] = await _launch(
            adapter,
            creation,
            image_id=image_id,
            runtime=client,
            token=token,
            steps=steps,
            ready_timeout=ready_timeout,
            connect=address,
        )
    return members


async def _launch(
    adapter: DockerAdapter,
    creation: Creation,
    *,
    image_id: str,
    runtime: PlannedRuntime,
    token: str,
    steps: _Steps,
    ready_timeout: float,
    connect: str | None,
) -> Member:
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
        {POINTS_FILE: runtime.points, TOKEN_FILE: token.encode("utf-8")},
    )
    await steps(f"start:{name}")
    await adapter.start_container(resource)
    control = await _control_address(adapter, resource)
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
    return Member(runtime=runtime, resource=resource, control=control, listen=listen)


async def _control_address(adapter: DockerAdapter, resource: Resource) -> str:
    """The host address of a runtime's control API, as the engine published it."""
    attrs = await adapter.inspect(resource)
    bindings = (attrs.get("NetworkSettings", {}).get("Ports") or {}).get(
        f"{CONTROL_PORT}/tcp"
    )
    if not bindings:
        raise BenchError(f"{resource.name} has no published control port")
    return f"127.0.0.1:{bindings[0]['HostPort']}"


async def _station_address(
    adapter: DockerAdapter,
    station: Member,
    ownership: Ownership,
    network: str,
) -> str:
    """The address a client connects to: the station's address on their network, and the
    port the station bound."""
    if station.listen is None:
        raise BenchError(f"{station.runtime.name} reports no listening address")
    attrs = await adapter.inspect(station.resource)
    networks = attrs.get("NetworkSettings", {}).get("Networks") or {}
    entry = networks.get(resource_name(ownership, network)) or {}
    address = entry.get("IPAddress")
    if not address:
        raise BenchError(f"{station.runtime.name} has no address on {network!r}")
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
    """Polls a runtime's readiness until `done` accepts it. Raises BenchError when the
    container stops, or when the time runs out, with what the runtime last reported."""
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
    """Keeps the logs and the record of each container a failed start created, before
    the rollback removes them. A container that cannot be read is skipped."""
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
