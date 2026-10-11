"""A runtime of the test bench: a client or a station, with its link parameters, its
points, and its resource limits (IMPLEMENTATION.md §5, §6).

The link parameters follow the upstream rules (`igs-iec104-link`, config.rs): t0, t1,
and t2 are whole seconds from 1 to 255, t2 is below t1, t3 is a whole number of seconds
of at least 1, and k and w are from 1 to 32767. The upstream also recommends w at most
two thirds of k and t3 at most 48 hours; those are recommendations, and the definition
does not refuse them."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt, model_validator

from igs_iec104_mcp.models.common import CommonAddress, Definition, Name
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

    @model_validator(mode="after")
    def _check_points(self) -> Instance:
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
