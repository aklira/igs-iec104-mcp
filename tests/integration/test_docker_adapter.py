"""The Docker adapter against a real engine (IMPLEMENTATION.md §6, task D1): labelled
networks and containers are created, found, and removed; resources that are not labelled
for the owner are never touched; a failed creation is rolled back; an image is the one
its digest names.

The tests pull one small image by its digest. Without an engine they skip, except in CI,
where a missing engine fails the run. Podman's Docker-compatible socket works too:
`DOCKER_HOST=unix:///run/user/<uid>/podman/podman.sock`."""

import secrets
from collections.abc import AsyncIterator
from typing import Any

import pytest
from docker.errors import NotFound

from docker import from_env  # type: ignore[attr-defined]
from igs_iec104_mcp.docker.adapter import (
    Conflict,
    DockerAdapter,
    ImageError,
)
from igs_iec104_mcp.docker.labels import INSTANCE, Ownership
from igs_iec104_mcp.docker.specs import (
    ContainerSpec,
    Limits,
    NetworkSpec,
    resource_name,
)

IMAGE = (
    "docker.io/library/busybox@sha256:"
    "f97baa533a26513c453a362be331a43eb60214302f449c16571b23cea141dce4"
)
UNKNOWN_DIGEST = "docker.io/library/busybox@sha256:" + "0" * 64


def _has_image(client: Any, reference: str) -> bool:
    try:
        client.images.get(reference)
    except NotFound:
        return False
    return True


def new_owner() -> Ownership:
    return Ownership(project="igs-test", bench="basic-pair", run=secrets.token_hex(16))


@pytest.fixture
async def owner(adapter: DockerAdapter) -> AsyncIterator[Ownership]:
    """A run of the test, whose resources are removed at the end, whatever happened."""
    run = new_owner()
    yield run
    await adapter.remove_owned(run)


async def image_identity(adapter: DockerAdapter) -> str:
    return await adapter.image_id(IMAGE)


def raw_client() -> Any:
    return from_env()


async def test_a_labelled_network_and_container_are_created_listed_and_removed(
    adapter: DockerAdapter, owner: Ownership
) -> None:
    image = await image_identity(adapter)
    async with adapter.creating(owner) as creation:
        network = await creation.network(NetworkSpec("lan"))
        container = await creation.container(
            ContainerSpec(instance="client-1", image=image, network="lan")
        )

    assert network.name == resource_name(owner, "lan")
    assert container.name == resource_name(owner, "client-1")
    listed = {resource.identity for resource in await adapter.owned(owner)}
    assert listed == {network.identity, container.identity}

    inspected = raw_client().containers.get(container.identity)
    labels = inspected.attrs["Config"]["Labels"]
    assert labels[INSTANCE] == "client-1"
    assert labels["dev.igs-iec104.mcp.run"] == owner.run

    removed = await adapter.remove_owned(owner)
    assert {resource.identity for resource in removed} == listed
    assert await adapter.owned(owner) == []


async def test_a_container_is_created_with_the_hardening_of_the_spec(
    adapter: DockerAdapter, owner: Ownership
) -> None:
    image = await image_identity(adapter)
    async with adapter.creating(owner) as creation:
        await creation.network(NetworkSpec("lan"))
        container = await creation.container(
            ContainerSpec(
                instance="server-1",
                image=image,
                network="lan",
                limits=Limits(cpus=0.5, memory_bytes=64 * 1024 * 1024, pids=64),
            )
        )

    host = raw_client().containers.get(container.identity).attrs["HostConfig"]
    assert host["Privileged"] is False
    assert host["ReadonlyRootfs"] is True
    assert not host.get("Binds")
    assert host["Memory"] == 64 * 1024 * 1024
    assert host["PidsLimit"] == 64
    assert host["NanoCpus"] == 500_000_000
    # Docker lists "ALL"; Podman lists each capability it drops.
    dropped = set(host.get("CapDrop") or [])
    assert "ALL" in dropped or {"CHOWN", "KILL", "NET_BIND_SERVICE"} <= dropped
    assert not host.get("CapAdd")


async def test_creating_again_finds_the_same_resources(
    adapter: DockerAdapter, owner: Ownership
) -> None:
    first, created_first = await adapter.create_network(NetworkSpec("lan"), owner)
    second, created_second = await adapter.create_network(NetworkSpec("lan"), owner)
    assert created_first is True
    assert created_second is False
    assert first.identity == second.identity
    await adapter.remove_owned(owner)


async def test_a_network_of_the_same_name_that_is_not_labelled_is_refused_and_kept(
    adapter: DockerAdapter, owner: Ownership
) -> None:
    name = resource_name(owner, "lan")
    foreign = raw_client().networks.create(name, driver="bridge")
    try:
        with pytest.raises(Conflict, match="does not belong to this run"):
            await adapter.create_network(NetworkSpec("lan"), owner)
        assert await adapter.remove_owned(owner) == []
        assert raw_client().networks.get(foreign.id).name == name
    finally:
        foreign.remove()


async def test_an_unlabelled_container_is_never_removed_by_the_owner(
    adapter: DockerAdapter, owner: Ownership
) -> None:
    image = await image_identity(adapter)
    name = resource_name(owner, "client-1")
    foreign = raw_client().containers.create(image, name=name)
    try:
        with pytest.raises(Conflict):
            await adapter.create_container(
                ContainerSpec(instance="client-1", image=image, network="lan"), owner
            )
        assert await adapter.remove_owned(owner) == []
        assert raw_client().containers.get(foreign.id).name == name
    finally:
        foreign.remove(force=True)


async def test_the_resources_of_another_run_are_not_removed(
    adapter: DockerAdapter, owner: Ownership
) -> None:
    other = new_owner()
    try:
        async with adapter.creating(other) as creation:
            kept = await creation.network(NetworkSpec("lan"))
        await adapter.remove_owned(owner)
        assert kept.identity in {r.identity for r in await adapter.owned(other)}
    finally:
        await adapter.remove_owned(other)


async def test_a_failed_creation_removes_the_resources_it_made(
    adapter: DockerAdapter, owner: Ownership
) -> None:
    unknown = "sha256:" + "0" * 64
    with pytest.raises(ImageError):
        async with adapter.creating(owner) as creation:
            await creation.network(NetworkSpec("lan"))
            await creation.container(
                ContainerSpec(instance="client-1", image=unknown, network="lan")
            )
    assert await adapter.owned(owner) == []


async def test_an_image_is_the_one_its_digest_names(adapter: DockerAdapter) -> None:
    image = await image_identity(adapter)
    assert image.startswith("sha256:")
    assert await adapter.image_id(image) == image
    with pytest.raises(ImageError, match="no image"):
        await adapter.image_id(UNKNOWN_DIGEST)
    with pytest.raises(ImageError, match="not a digest"):
        await adapter.image_id("busybox:latest")
    with pytest.raises(ImageError, match="no image"):
        await adapter.image_id("sha256:" + "1" * 64)
