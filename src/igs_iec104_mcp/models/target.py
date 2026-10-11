"""
An external target: an IEC 60870-5-104 peer the bench does not manage (IMPLEMENTATION.md
§5, §9).

The controller may perform the operations the target lists, and must not assume it can
restart, reconfigure, or destroy it. Key material is never written in a definition: the
TLS fields name secrets.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from igs_iec104_mcp.models.common import CommonAddress, Definition, Name, SecretRef

STATE_CHANGING: frozenset[str] = frozenset(
    {"command", "setpoint", "clock_sync", "counter_freeze", "counter_reset"}
)


class Operation(StrEnum):
    """
    The protocol operations a target may be asked for (§9). The state-changing ones need
        confirmation.
    """

    START_TRANSFER = "start_transfer"
    STOP_TRANSFER = "stop_transfer"
    INTERROGATE = "interrogate"
    READ = "read"
    COUNTER_INTERROGATION = "counter_interrogation"
    CLOCK_SYNC = "clock_sync"
    TEST_COMMAND = "test_command"
    COMMAND = "command"
    SETPOINT = "setpoint"
    COUNTER_FREEZE = "counter_freeze"
    COUNTER_RESET = "counter_reset"


class Endpoint(Definition):
    """Where the target listens or is reached."""

    host: str = Field(
        min_length=1, max_length=253, description="A host name or an address."
    )
    port: StrictInt = Field(ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _no_spaces(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError(
                f"the host {value!r} contains a space: write the host name only"
            )
        return value


class TlsSecrets(Definition):
    """
    The secrets of a secured connection: the names of the certificates and keys, never
        their content.
    """

    ca: SecretRef | None = Field(
        default=None, description="The certificate authority that signs the peer."
    )
    certificate: SecretRef
    key: SecretRef


class OptIn(Definition):
    """
    A reviewed opt-in: the state-changing operations the target may receive without a
        human confirmation at run time (§9).
    """

    reviewer: str = Field(
        min_length=1, max_length=120, description="Who reviewed the opt-in."
    )
    reviewed_on: str = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="The date of the review, as YYYY-MM-DD.",
    )
    operations: list[Operation] = Field(
        min_length=1, description="The state-changing operations the opt-in covers."
    )

    @field_validator("reviewed_on", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> Any:
        # A YAML date, written without quotes, is read as a date: keep its text.
        return value.isoformat() if isinstance(value, date) else value

    @field_validator("operations")
    @classmethod
    def _state_changing(cls, value: list[Operation]) -> list[Operation]:
        if len(set(value)) != len(value):
            raise ValueError("an operation is listed twice: list each one once")
        for operation in value:
            if operation.value not in STATE_CHANGING:
                raise ValueError(
                    f"{operation.value!r} does not change state: an opt-in covers "
                    "state-changing operations only"
                )
        return value


class ExternalTarget(Definition):
    """A peer outside the bench, with the operations the controller may perform on "
    "it."""

    name: Name
    role: Literal["client", "server"]
    network: Name = Field(description="The external network the target is reached on.")
    endpoint: Endpoint
    common_address: CommonAddress
    tls: TlsSecrets | None = None
    allowed_operations: list[Operation] = Field(default_factory=list)
    opt_in: OptIn | None = Field(
        default=None,
        description="The reviewed opt-in, for a client that cannot confirm.",
    )

    @field_validator("allowed_operations")
    @classmethod
    def _unique(cls, value: list[Operation]) -> list[Operation]:
        if len(set(value)) != len(value):
            raise ValueError(
                "an operation is listed twice: list each allowed operation once"
            )
        return value

    @model_validator(mode="after")
    def _opt_in_is_allowed(self) -> ExternalTarget:
        if self.opt_in is None:
            return self
        unlisted = [
            op.value
            for op in self.opt_in.operations
            if op not in self.allowed_operations
        ]
        if unlisted:
            raise ValueError(
                f"the opt-in covers {', '.join(unlisted)}, which the target does not "
                "allow: list the operation in allowed_operations"
            )
        return self


def undeclared(target: ExternalTarget, required: set[Operation]) -> list[str]:
    """The operations a step needs that the target does not list, by name and in order.

    An empty list means the target allows every operation the step needs. This is the
    one rule of the control plane for operations on an external target: a scenario that
    needs an operation the target does not list is refused, before any run starts (§5,
    §9).
    """
    allowed = set(target.allowed_operations)
    return sorted(op.value for op in required if op not in allowed)
