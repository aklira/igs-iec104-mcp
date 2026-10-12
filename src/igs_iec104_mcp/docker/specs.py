"""The networks and containers the controller creates, as data that is checked before
any call to the engine (IMPLEMENTATION.md §6).

A protocol container is described by what it may have, not by what it may not: there is
no field for a bind mount, a privileged flag, the host network, or an extra capability.
So the Docker socket cannot reach a runtime through the spec, and the spec is always
built with the same hardening. The image is named by its digest, never by a tag that can
move."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field

from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.models.common import check_name

# A repository digest (`repository@sha256:<64 hex>`) or an image identity
# (`sha256:<64 hex>`). A tag is refused: it can be moved after the check.
IMAGE_PATTERN = re.compile(
    r"^(?:[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64})$"
)
# Docker names are at most 63 characters here, to keep them readable in a list.
MAX_NAME_LENGTH = 63
MIN_MEMORY = 16 * 1024 * 1024
MAX_PIDS = 4096
MAX_CPUS = 64.0
MIN_PIDS = 16
# The runtime's control API listens on this port inside its container. The engine
# publishes it on 127.0.0.1 at a port it picks, which the controller reads back (D2).
CONTROL_PORT = 8080
# The network of a bench that the host can reach through its relays. It is not internal:
# a published port needs a network with a route. Only the relays are on it (§6, D-018).
CONTROL_NETWORK = "control"
# The directory of the runtime's files (its control token and its points). The image
# creates it, owned by the runtime's user; the files are copied in before the container
# starts, so no mount is used: a mount made at start would hide the copied files.
RUN_DIRECTORY = "/run/igs"
RUNTIME_UID = 10001
# The user of a relay and of a fault proxy, which run from the relay image
# (relay.Dockerfile).
RELAY_UID = 10002
# The runtime is hardened by default: no privilege escalation, with the read-only root
# filesystem and the dropped capabilities set in the container spec.
SECURITY_OPTIONS = ("no-new-privileges:true",)


class SpecError(ValueError):
    """A spec is not valid: the message names the field and what it may be."""


@dataclass(frozen=True)
class Limits:
    """The resources a container may use: CPUs, memory in bytes, and processes."""

    cpus: float = 1.0
    memory_bytes: int = 256 * 1024 * 1024
    pids: int = 256

    def __post_init__(self) -> None:
        if not 0 < self.cpus <= MAX_CPUS:
            raise SpecError(
                f"cpus {self.cpus} is out of range: use above 0 up to {MAX_CPUS:g}"
            )
        if self.memory_bytes < MIN_MEMORY:
            raise SpecError(
                f"memory of {self.memory_bytes} bytes is too small: use at least "
                f"{MIN_MEMORY} (16 MiB)"
            )
        if not MIN_PIDS <= self.pids <= MAX_PIDS:
            raise SpecError(
                f"pids {self.pids} is out of range: use {MIN_PIDS} to {MAX_PIDS}"
            )


@dataclass(frozen=True)
class NetworkSpec:
    """A bridge network of a test bench, by its name in the bench. An internal network
    has no route outside the host: it is used unless the bench declares an external
    target (§6)."""

    name: str
    internal: bool = True

    def __post_init__(self) -> None:
        check_plain_name(self.name, "network")


@dataclass(frozen=True)
class PortPublication:
    """A container port the engine publishes on the host: a host address and a host port
    that the caller names. The engine picks no port here (§6)."""

    container_port: int
    bind: str
    host_port: int

    def __post_init__(self) -> None:
        for value, what in (
            (self.container_port, "container"),
            (self.host_port, "host"),
        ):
            if not 1 <= value <= 65535:
                raise SpecError(
                    f"the {what} port {value} is out of range: use 1 to 65535"
                )
        try:
            ipaddress.ip_address(self.bind)
        except ValueError as error:
            raise SpecError(
                f"{self.bind!r} is not an IP address to publish on"
            ) from error


@dataclass(frozen=True)
class ContainerSpec:
    """A runtime container: its instance, its image, its network (by name in the
    bench), its limits, and the arguments of the runtime. It has no bind mount, no
    privilege, and no port, unless `publish_control` is set (a relay's port)"""

    instance: str
    image: str
    network: str
    limits: Limits = field(default_factory=Limits)
    arguments: tuple[str, ...] = ()
    entrypoint: tuple[str, ...] | None = None
    publish_control: bool = False
    publish: PortPublication | None = None

    def __post_init__(self) -> None:
        check_plain_name(self.instance, "instance")
        check_plain_name(self.network, "network")
        if self.publish_control and self.publish is not None:
            raise SpecError(
                "a container publishes its control port or one port, not both"
            )
        if not IMAGE_PATTERN.fullmatch(self.image):
            raise SpecError(
                f"image {self.image!r} is not a digest: write the repository digest "
                "('repository@sha256:...'), or the image identity ('sha256:...'), "
                "never a tag"
            )


def check_plain_name(name: str, what: str) -> None:
    """A Docker name: the name pattern of a definition, and at most 63 characters."""
    try:
        check_name(name)
    except ValueError as error:
        raise SpecError(f"the {what} name: {error}") from error
    if len(name) > MAX_NAME_LENGTH:
        raise SpecError(f"the {what} name {name!r} is longer than {MAX_NAME_LENGTH}")


def part_of(name: str) -> str:
    """The part of a resource name that `resource_name` built: the instance or the
    network. Raises SpecError for a name the controller did not build."""
    prefix, run, part = [*name.split("-", 2), "", ""][:3]
    if prefix != "igsmcp" or len(run) != 12 or not part:
        raise SpecError(f"{name!r} is not a name of this controller")
    return part


def resource_name(ownership: Ownership, part: str) -> str:
    """The Docker name of a resource of a run: a prefix that names the run, then the
    part (a network or an instance). The labels, not the name, decide ownership."""
    run = ownership.run[:12]
    name = f"igsmcp-{run}-{part}"
    check_plain_name(name, "resource")
    return name
