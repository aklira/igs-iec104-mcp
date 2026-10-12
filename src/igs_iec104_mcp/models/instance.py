"""A runtime of the test bench: a client or a station, with its link parameters, its
points, and its resource limits (IMPLEMENTATION.md §5, §6).

The link parameters follow the upstream rules (`igs-iec104-link`, config.rs): t0, t1,
and t2 are whole seconds from 1 to 255, t2 is below t1, t3 is a whole number of seconds
of at least 1, and k and w are from 1 to 32767. The upstream also recommends w at most
two thirds of k and t3 at most 48 hours; those are recommendations, and the definition
does not refuse them."""

from __future__ import annotations

import ipaddress
from typing import Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from igs_iec104_mcp.models.common import (
    CommonAddress,
    Definition,
    Name,
    SecretRef,
)
from igs_iec104_mcp.models.points import Point

MAX_TIMEOUT_SECONDS: int = 255
MAX_WINDOW: int = 32_767


class LinkParameters(Definition):
    """
    The timeouts (whole seconds) and the window sizes of the link, with the defaults of
        §9.6.
    """

    t0: StrictInt = Field(
        default=30,
        ge=1,
        le=MAX_TIMEOUT_SECONDS,
        description="Connection establishment, in seconds.",
    )
    t1: StrictInt = Field(
        default=15,
        ge=1,
        le=MAX_TIMEOUT_SECONDS,
        description="Sending or testing APDUs, in seconds.",
    )
    t2: StrictInt = Field(
        default=10,
        ge=1,
        le=MAX_TIMEOUT_SECONDS,
        description="Acknowledgements without data, in seconds; below t1.",
    )
    t3: StrictInt = Field(
        default=20, ge=1, description="Test frames in a long idle state, in seconds."
    )
    k: StrictInt = Field(
        default=12,
        ge=1,
        le=MAX_WINDOW,
        description="Unacknowledged I frames the sender may have.",
    )
    w: StrictInt = Field(
        default=8,
        ge=1,
        le=MAX_WINDOW,
        description="I frames received before an acknowledgement.",
    )

    @model_validator(mode="after")
    def _check_order(self) -> LinkParameters:
        if self.t2 >= self.t1:
            raise ValueError(
                f"t2 ({self.t2} s) must be below t1 ({self.t1} s): an acknowledgement "
                f"is due before a send times out"
            )
        return self


class ResourceLimits(Definition):
    """The limits of one runtime's container (§6): CPU, memory, processes, events, and
    execution time."""

    cpu: float = Field(default=0.5, gt=0, le=8, description="CPU share, in cores.")
    memory_mb: StrictInt = Field(
        default=256, ge=16, le=4096, description="Memory, in megabytes."
    )
    pids: StrictInt = Field(
        default=64, ge=1, le=512, description="Processes and threads."
    )
    event_buffer: StrictInt = Field(
        default=10_000, ge=1, le=1_000_000, description="Events kept for the run."
    )
    execution_seconds: StrictInt = Field(
        default=3600, ge=1, le=86_400, description="Execution time, in seconds."
    )


class Publication(Definition):
    """A station's IEC 104 port made reachable from outside Docker (§6, task X2).

    The station listens on `port` inside the bench, so that port is imposed, and the
    engine publishes it on `bind`:`host_port`. `advertise` is the endpoint an external
    client is given: it is declared, never discovered.
    """

    port: StrictInt = Field(
        ge=1, le=65535, description="The station's port in the bench."
    )
    host_port: StrictInt = Field(
        ge=1, le=65535, description="The host port the engine publishes."
    )
    bind: str = Field(
        default="127.0.0.1",
        description="The host address the port is published on. The default keeps it "
        "on the loopback; a wider address is an explicit choice.",
    )
    advertise: str = Field(
        min_length=1,
        max_length=253,
        description="The host name or address an external client connects to.",
    )

    @field_validator("bind")
    @classmethod
    def _an_address(cls, value: str) -> str:
        try:
            ipaddress.ip_address(value)
        except ValueError as error:
            raise ValueError(f"{value!r} is not an IP address to publish on") from error
        return value

    @field_validator("advertise")
    @classmethod
    def _no_spaces(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError(
                f"the advertised endpoint {value!r} contains a space: write the host "
                "only, and give the port in host_port"
            )
        return value


MAX_INTERMEDIATES: int = 8


class InstanceTls(Definition):
    """The TLS of a runtime's connections (IMPLEMENTATION.md §6, §9).

    Every file is a secret reference: the controller holds the material, and the
    definition names it. The certificate, the key, and the trust anchor are DER files;
    the intermediates link the certificate to the trust anchor, in chain order.
    """

    certificate: SecretRef = Field(
        description="The runtime's certificate, as a secret reference (DER)."
    )
    key: SecretRef = Field(
        description="The private key of the certificate, as a secret reference (DER)."
    )
    trust: SecretRef = Field(
        description="The root certificate that signs the peer's certificate (DER)."
    )
    intermediates: list[SecretRef] = Field(
        default_factory=list,
        max_length=MAX_INTERMEDIATES,
        description="The intermediate certificates, in chain order (DER).",
    )
    server_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=253,
        description="For a client: the name the station's certificate is checked "
        "against. Without it, the client checks the address it connects to.",
    )

    @field_validator("server_name")
    @classmethod
    def _no_spaces(cls, value: str | None) -> str | None:
        if value is not None and any(character.isspace() for character in value):
            raise ValueError(f"the server name {value!r} contains a space")
        return value


class Instance(Definition):
    """A runtime of the bench: a client of a station, or a station."""

    name: Name
    role: Literal["client", "server"]
    network: Name = Field(
        description="The network the runtime is on; it must be declared by the bench."
    )
    common_address: CommonAddress
    parameters: LinkParameters = Field(default_factory=LinkParameters)
    points: list[Point] = Field(default_factory=list)
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    publish: Publication | None = Field(
        default=None,
        description="Make a station reachable from outside Docker (§6). Stations only.",
    )
    tls: InstanceTls | None = Field(
        default=None,
        description="Secure the runtime's connections with TLS (§6, §9).",
    )

    @model_validator(mode="after")
    def _check_points(self) -> Instance:
        if self.role == "client" and self.publish is not None:
            raise ValueError(
                f"instance {self.name!r} is a client: only a station can be published"
            )
        if (
            self.role == "server"
            and self.tls is not None
            and self.tls.server_name is not None
        ):
            raise ValueError(
                f"instance {self.name!r} is a station: its name is its certificate's, "
                "so server_name is for a client"
            )
        if self.role == "client" and self.points:
            raise ValueError(
                f"instance {self.name!r} is a client, which serves no points: move "
                f"the points to a server"
            )
        seen: set[int] = set()
        for point in self.points:
            if point.address in seen:
                raise ValueError(
                    f"instance {self.name!r} has two points at address "
                    f"{point.address}: each address is used once"
                )
            seen.add(point.address)
        return self
