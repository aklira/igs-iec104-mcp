"""The Docker specs and labels, checked before any call to the engine (IMPLEMENTATION.md
§6): a tag is refused, the hardening is fixed, the limits are bounded, and a resource
belongs to its owner only by its labels."""

import dataclasses

import pytest

from igs_iec104_mcp.docker.labels import (
    INSTANCE,
    MANAGED,
    Ownership,
    OwnershipError,
    owns,
)
from igs_iec104_mcp.docker.specs import (
    ContainerSpec,
    Limits,
    NetworkSpec,
    SpecError,
    resource_name,
)

RUN = "0123456789abcdef0123456789abcdef"
DIGEST = "sha256:" + "a" * 64
REPOSITORY_DIGEST = "docker.io/library/busybox@sha256:" + "b" * 64


def owner(run: str = RUN) -> Ownership:
    return Ownership(project="demo", bench="basic-pair", run=run)


def test_an_owner_needs_names_and_a_run_identity() -> None:
    with pytest.raises(OwnershipError, match="not a valid name"):
        Ownership(project="Demo", bench="basic-pair", run=RUN)
    with pytest.raises(OwnershipError, match="not a run identity"):
        Ownership(project="demo", bench="basic-pair", run="run-1")


def test_the_labels_name_the_owner_and_the_instance() -> None:
    labels = owner().labels(instance="client-1")
    assert labels[MANAGED] == "true"
    assert labels[INSTANCE] == "client-1"
    assert "dev.igs-iec104.mcp.project" in labels
    assert INSTANCE not in owner().labels()


def test_a_resource_belongs_to_its_owner_only_by_its_labels() -> None:
    assert owns(owner().labels(instance="x"), owner())
    assert not owns(None, owner())
    assert not owns({}, owner())
    assert not owns(owner(run="f" * 32).labels(), owner())
    assert not owns({"dev.igs-iec104.mcp.project": "demo"}, owner())
    unmanaged = {
        key: value for key, value in owner().labels().items() if key != MANAGED
    }
    assert not owns(unmanaged, owner())


def test_a_name_of_a_resource_carries_the_run_and_its_part() -> None:
    assert resource_name(owner(), "lan") == "igsmcp-0123456789ab-lan"
    with pytest.raises(SpecError, match="not a valid name"):
        resource_name(owner(), "LAN")


def test_a_network_name_is_checked() -> None:
    with pytest.raises(SpecError, match="network name"):
        NetworkSpec("Bad Name")
    assert NetworkSpec("lan").internal is True


@pytest.mark.parametrize(
    "image", ["busybox", "busybox:latest", "busybox@sha256:" + "a" * 63, "sha256:xyz"]
)
def test_an_image_named_by_a_tag_or_a_bad_digest_is_refused(image: str) -> None:
    with pytest.raises(SpecError, match="never a tag"):
        ContainerSpec(instance="client-1", image=image, network="lan")


@pytest.mark.parametrize("image", [DIGEST, REPOSITORY_DIGEST])
def test_an_image_named_by_a_digest_is_accepted(image: str) -> None:
    spec = ContainerSpec(instance="client-1", image=image, network="lan")
    assert spec.image == image


def test_a_container_has_no_field_for_a_mount_a_privilege_or_a_capability() -> None:
    names = {field.name for field in dataclasses.fields(ContainerSpec)}
    assert names == {
        "instance",
        "image",
        "network",
        "limits",
        "arguments",
        "publish_control",
    }
    with pytest.raises(TypeError):
        ContainerSpec(  # type: ignore[call-arg]
            instance="client-1", image=DIGEST, network="lan", privileged=True
        )
    with pytest.raises(TypeError):
        ContainerSpec(  # type: ignore[call-arg]
            instance="client-1", image=DIGEST, network="lan", binds=["/:/host"]
        )


def test_the_limits_are_bounded() -> None:
    assert Limits().pids == 256
    with pytest.raises(SpecError, match="cpus"):
        Limits(cpus=0)
    with pytest.raises(SpecError, match="cpus"):
        Limits(cpus=65)
    with pytest.raises(SpecError, match="memory"):
        Limits(memory_bytes=1024)
    with pytest.raises(SpecError, match="pids"):
        Limits(pids=8)
    with pytest.raises(SpecError, match="pids"):
        Limits(pids=5000)


def test_a_container_checks_its_names_and_limits() -> None:
    with pytest.raises(SpecError, match="instance name"):
        ContainerSpec(instance="Client 1", image=DIGEST, network="lan")
    with pytest.raises(SpecError, match="cpus"):
        ContainerSpec(
            instance="client-1", image=DIGEST, network="lan", limits=Limits(cpus=0)
        )
