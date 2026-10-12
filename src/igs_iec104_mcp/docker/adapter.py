"""The Docker adapter: the networks and containers of a run, created, found, and removed
through the Engine API (IMPLEMENTATION.md §6).

The Docker SDK is synchronous, so every call runs in a worker thread, off the event
loop. Each call checks the labels of what it finds, so a resource with the right name
but without the owner's labels is refused, never removed. A creation that fails part way
is rolled back: the resources it created are removed, in reverse order, and nothing
else."""

from __future__ import annotations

import asyncio
import io
import tarfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, TypeVar

from docker.errors import APIError, DockerException, NotFound

# The SDK ships no type hints (see pyproject.toml): its client is typed Any here.
from docker import from_env  # type: ignore[attr-defined]
from igs_iec104_mcp.docker.labels import (
    BENCH,
    INSTANCE,
    MANAGED,
    PROJECT,
    RUN,
    Ownership,
    OwnershipError,
    owns,
)
from igs_iec104_mcp.docker.specs import (
    CONTROL_PORT,
    IMAGE_PATTERN,
    RUN_DIRECTORY,
    RUNTIME_UID,
    SECURITY_OPTIONS,
    ContainerSpec,
    NetworkSpec,
    resource_name,
)

T = TypeVar("T")
Kind = Literal["network", "container"]


class DockerError(Exception):
    """The engine failed, or is not reachable. The message says what happened."""


class ImageError(DockerError):
    """An image is not the one its digest names, or is not on the engine."""


class Conflict(DockerError):
    """A resource of the same name exists and does not belong to this owner."""


@dataclass(frozen=True)
class Resource:
    """A network or a container, by its engine identity and its Docker name."""

    kind: Kind
    identity: str
    name: str


@dataclass
class Creation:
    """What a block of creations made. `created` lists only the resources the block
    created itself: a resource that was already there, and belonged to the owner, is
    kept when the block fails."""

    adapter: DockerAdapter
    ownership: Ownership
    created: list[Resource]

    async def network(self, spec: NetworkSpec) -> Resource:
        resource, created = await self.adapter.create_network(spec, self.ownership)
        if created:
            self.created.append(resource)
        return resource

    async def container(self, spec: ContainerSpec) -> Resource:
        resource, created = await self.adapter.create_container(spec, self.ownership)
        if created:
            self.created.append(resource)
        return resource


class DockerAdapter:
    """The resources of runs on one Docker Engine (or a Docker-compatible one, such as
    Podman's socket)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def connect(cls) -> DockerAdapter:
        """The engine named by the environment (`DOCKER_HOST`), or the default
        socket."""
        try:
            return cls(from_env())
        except (DockerException, OSError) as error:
            raise DockerError(f"no Docker engine: {error}") from error

    async def ping(self) -> None:
        """Raises DockerError when the engine does not answer."""
        if not await self._call(self._client.ping):
            raise DockerError("the Docker engine did not answer the ping")

    async def image_id(self, reference: str) -> str:
        """The identity of an image, when it is the image the digest names. Raises
        ImageError for a tag, a missing image, or an image that is not the digest's."""
        if not IMAGE_PATTERN.fullmatch(reference):
            raise ImageError(
                f"{reference!r} is not a digest: name the image by its digest, "
                "never a tag"
            )
        return await self._call(lambda: self._image_id(reference))

    async def create_network(
        self, spec: NetworkSpec, ownership: Ownership
    ) -> tuple[Resource, bool]:
        """The network of the owner, created, or found when it already exists for this
        owner. The flag tells which: True when it was created now."""
        return await self._call(lambda: self._create_network(spec, ownership))

    async def create_container(
        self, spec: ContainerSpec, ownership: Ownership
    ) -> tuple[Resource, bool]:
        """The container of the owner's instance, created with the hardening of §6, or
        found when it exists for this owner. The flag is True when it was created
        now."""
        return await self._call(lambda: self._create_container(spec, ownership))

    async def owned(self, ownership: Ownership) -> list[Resource]:
        """Every network and container that belongs to the owner."""
        return await self._call(lambda: self._owned(ownership))

    async def remove_owned(self, ownership: Ownership) -> list[Resource]:
        """Removes the containers, then the networks, of the owner. Nothing that is not
        labelled for the owner is touched. Returns what was removed."""
        return await self._call(lambda: self._remove_owned(ownership))

    async def owners(self, project: str) -> list[Ownership]:
        """The owners of the labelled resources of a project, one per run and bench, as
        the labels name them. A resource whose labels name no valid owner is skipped."""
        return await self._call(lambda: self._owners(project))

    @asynccontextmanager
    async def creating(self, ownership: Ownership) -> AsyncIterator[Creation]:
        """A block of creations for one owner. When it fails, the resources it created
        are removed, and the error is raised again. If a removal fails too, the error
        says which resources remain."""
        creation = Creation(self, ownership, [])
        try:
            yield creation
        except BaseException as error:
            leftover = await self._rollback(creation)
            if leftover:
                raise DockerError(
                    "the creation failed and some resources remain: "
                    + ", ".join(leftover)
                ) from error
            raise

    async def _rollback(self, creation: Creation) -> list[str]:
        leftover: list[str] = []
        for resource in reversed(creation.created):
            try:
                await self._call(
                    partial(self._remove_resource, resource, creation.ownership)
                )
            except DockerError:
                leftover.append(f"{resource.kind} {resource.name}")
        creation.created.clear()
        return leftover

    async def _call(self, call: Callable[[], T]) -> T:
        """Runs a synchronous call in a worker thread, and turns the engine's errors
        into DockerError. A socket failure is an OSError, so it is caught as well."""
        try:
            return await asyncio.to_thread(call)
        except (APIError, DockerException, OSError) as error:
            raise DockerError(f"the Docker engine failed: {error}") from error

    async def put_files(
        self,
        resource: Resource,
        files: dict[str, bytes],
        owner: int = RUNTIME_UID,
    ) -> None:
        """Copies files into the run directory of a created container, before it
        starts. Each file is readable by the owner's user alone (the runtime's, unless
        another is given). A name is a plain file name."""
        for name in files:
            if not name or "/" in name or name in {".", ".."}:
                raise DockerError(f"{name!r} is not a plain file name")
        await self._call(partial(self._put_files, resource, files, owner))

    async def connect_network(
        self, resource: Resource, network: str, ownership: Ownership
    ) -> None:
        """Joins a created container to a second network of the owner. The network must
        be the owner's, by its labels."""
        await self._call(partial(self._connect_network, resource, network, ownership))

    def _connect_network(
        self, resource: Resource, network: str, ownership: Ownership
    ) -> None:
        found = self._client.networks.get(network)
        if not owns(found.attrs.get("Labels"), ownership):
            raise Conflict(f"network {network!r} is not this run's: refused")
        found.connect(self._container(resource))

    async def start_container(self, resource: Resource) -> None:
        await self._call(self._container(resource).start)

    async def stop_container(self, resource: Resource, grace: int) -> None:
        """Stops a container: SIGTERM, then SIGKILL after `grace` seconds."""
        await self._call(partial(self._container(resource).stop, timeout=grace))

    async def inspect(self, resource: Resource) -> dict[str, Any]:
        """The engine's record of a container: its state, ports, and networks."""
        return await self._call(lambda: dict(self._container(resource).attrs))

    async def logs(self, resource: Resource) -> bytes:
        return await self._call(
            lambda: bytes(self._container(resource).logs(stdout=True, stderr=True))
        )

    def _container(self, resource: Resource) -> Any:
        return self._client.containers.get(resource.identity)

    def _put_files(
        self, resource: Resource, files: dict[str, bytes], owner: int
    ) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o400
                info.uid = info.gid = owner
                archive.addfile(info, io.BytesIO(data))
        self._container(resource).put_archive(RUN_DIRECTORY, buffer.getvalue())

    def _image_id(self, reference: str) -> str:
        try:
            image = self._client.images.get(reference)
        except NotFound as error:
            raise ImageError(
                f"no image {reference!r} on the engine: pull or build it, then name "
                "its digest"
            ) from error
        if reference.startswith("sha256:"):
            if image.id != reference:
                raise ImageError(
                    f"the image {reference!r} is not the one on the engine"
                )
            return str(image.id)
        digest = reference.split("@", 1)[1]
        repo_digests: list[str] = image.attrs.get("RepoDigests") or []
        if not any(item.endswith("@" + digest) for item in repo_digests):
            raise ImageError(
                f"the image on the engine is not the one {reference!r} names"
            )
        return str(image.id)

    def _create_network(
        self, spec: NetworkSpec, ownership: Ownership
    ) -> tuple[Resource, bool]:
        name = resource_name(ownership, spec.name)
        existing = self._find_network(name)
        if existing is not None:
            labels = existing.attrs.get("Labels") or {}
            if not owns(labels, ownership):
                raise Conflict(
                    f"a network named {name!r} exists and does not belong to this run: "
                    "refused"
                )
            if bool(existing.attrs.get("Internal")) != spec.internal:
                raise Conflict(
                    f"the network {name!r} exists with other connectivity: refused"
                )
            return Resource("network", str(existing.id), name), False
        network = self._client.networks.create(
            name,
            driver="bridge",
            internal=spec.internal,
            labels=ownership.labels(),
            check_duplicate=True,
        )
        return Resource("network", str(network.id), name), True

    def _create_container(
        self, spec: ContainerSpec, ownership: Ownership
    ) -> tuple[Resource, bool]:
        name = resource_name(ownership, spec.instance)
        network = resource_name(ownership, spec.network)
        existing = self._find_container(name)
        if existing is not None:
            labels = _container_labels(existing)
            if not owns(labels, ownership) or labels.get(INSTANCE) != spec.instance:
                raise Conflict(
                    f"a container named {name!r} exists and is not this instance of "
                    "this run: refused"
                )
            return Resource("container", str(existing.id), name), False
        image = self._image_id(spec.image)
        container = self._client.containers.create(
            image=image,
            name=name,
            labels=ownership.labels(instance=spec.instance),
            network=network,
            command=list(spec.arguments) or None,
            entrypoint=list(spec.entrypoint) if spec.entrypoint else None,
            privileged=False,
            cap_drop=["ALL"],
            security_opt=list(SECURITY_OPTIONS),
            mem_limit=spec.limits.memory_bytes,
            nano_cpus=round(spec.limits.cpus * 1_000_000_000),
            pids_limit=spec.limits.pids,
            ports=_ports_of(spec),
        )
        return Resource("container", str(container.id), name), True

    def _find_network(self, name: str) -> Any:
        for network in self._client.networks.list(filters={"name": name}):
            if network.name == name:
                return network
        return None

    def _find_container(self, name: str) -> Any:
        for container in self._client.containers.list(all=True, filters={"name": name}):
            if container.name == name:
                return container
        return None

    def _owned(self, ownership: Ownership) -> list[Resource]:
        found: list[Resource] = []
        for network in self._client.networks.list(filters=ownership.filters()):
            if owns(network.attrs.get("Labels"), ownership):
                found.append(Resource("network", str(network.id), str(network.name)))
        for container in self._client.containers.list(
            all=True, filters=ownership.filters()
        ):
            if owns(_container_labels(container), ownership):
                found.append(
                    Resource("container", str(container.id), str(container.name))
                )
        return found

    def _owners(self, project: str) -> list[Ownership]:
        selector = {"label": [f"{MANAGED}=true", f"{PROJECT}={project}"]}
        labelled: list[dict[str, str]] = [
            network.attrs.get("Labels") or {}
            for network in self._client.networks.list(filters=selector)
        ]
        labelled += [
            _container_labels(container)
            for container in self._client.containers.list(all=True, filters=selector)
        ]
        owners: dict[tuple[str, str], Ownership] = {}
        for labels in labelled:
            bench, run = labels.get(BENCH), labels.get(RUN)
            if bench is None or run is None:
                continue
            try:
                owners[(run, bench)] = Ownership(project=project, bench=bench, run=run)
            except OwnershipError:
                continue
        return [owners[key] for key in sorted(owners)]

    def _remove_owned(self, ownership: Ownership) -> list[Resource]:
        removed: list[Resource] = []
        owned = self._owned(ownership)
        # Containers go first: a network that still has a container cannot be removed.
        for resource in sorted(owned, key=lambda r: r.kind != "container"):
            self._remove_resource(resource, ownership)
            removed.append(resource)
        return removed

    def _remove_resource(self, resource: Resource, ownership: Ownership) -> None:
        """Removes one resource by its identity, after its labels are checked. A
        resource that is already gone is not an error."""
        try:
            if resource.kind == "container":
                container = self._client.containers.get(resource.identity)
                if not owns(_container_labels(container), ownership):
                    raise Conflict(f"container {resource.name!r} is not this run's")
                container.remove(force=True)
            else:
                network = self._client.networks.get(resource.identity)
                if not owns(network.attrs.get("Labels"), ownership):
                    raise Conflict(f"network {resource.name!r} is not this run's")
                network.remove()
        except NotFound:
            return


def _container_labels(container: Any) -> dict[str, str]:
    labels: dict[str, str] = (container.attrs.get("Config") or {}).get("Labels") or {}
    return labels


def _ports_of(spec: ContainerSpec) -> dict[str, Any]:
    """The ports the engine publishes: the control port at an engine-chosen port on the
    loopback, or one named port at the address and port the spec names."""
    if spec.publish_control:
        return {f"{CONTROL_PORT}/tcp": ("127.0.0.1", None)}
    if spec.publish is not None:
        publication = spec.publish
        return {
            f"{publication.container_port}/tcp": (
                publication.bind,
                publication.host_port,
            )
        }
    return {}
