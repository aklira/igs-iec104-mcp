"""The external targets of a bench: their static check, and a connectivity probe.

Task X1 (IMPLEMENTATION.md §5, §9). A check reads the target's definition and reports
what the control plane allows on it; it makes no connection. A probe is a TCP connection
to the endpoint, opened and closed again, with no IEC 60870-5-104 frame sent. It runs
only when the caller asks for it, and only for a target the stored bench declares, so a
request cannot reach an address the bench does not name.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from pydantic import BaseModel, Field

from igs_iec104_mcp.definitions.service import ServiceError
from igs_iec104_mcp.definitions.store import Missing, ProjectStore, StoreError
from igs_iec104_mcp.models.common import DefinitionError
from igs_iec104_mcp.models.target import Endpoint, ExternalTarget

PROBE_MIN_SECONDS: float = 0.1
PROBE_MAX_SECONDS: float = 10.0
PROBE_DEFAULT_SECONDS: float = 3.0


class Probe(BaseModel):
    """The outcome of a connectivity probe: whether the endpoint accepted a TCP
    connection, how long it took, and what was seen."""

    reachable: bool
    seconds: float
    detail: str


class TargetReport(BaseModel):
    """A target as the control plane reads it: its endpoint, its common address, whether
    it is secured, the operations it allows, and the probe when one was asked for."""

    target: str
    role: str
    host: str
    port: int
    common_address: int
    secured: bool
    allowed_operations: list[str] = Field(default_factory=list)
    probe: Probe | None = None


def report_of(target: ExternalTarget) -> TargetReport:
    """The static report of a target: its definition, and no connection."""
    return TargetReport(
        target=target.name,
        role=target.role,
        host=target.endpoint.host,
        port=target.endpoint.port,
        common_address=target.common_address,
        secured=target.tls is not None,
        allowed_operations=sorted(op.value for op in target.allowed_operations),
    )


async def probe_tcp(endpoint: Endpoint, timeout: float) -> Probe:
    """A TCP connection to the endpoint, closed again at once. No IEC frame is sent. A
    secured target is probed on its TCP port only: the handshake is not attempted."""
    began = asyncio.get_running_loop().time()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(endpoint.host, endpoint.port), timeout
        )
    except TimeoutError:
        return Probe(
            reachable=False,
            seconds=_elapsed(began),
            detail=f"no answer within {timeout:g} s",
        )
    except OSError as error:
        return Probe(
            reachable=False,
            seconds=_elapsed(began),
            detail=f"the connection failed: {error.strerror or error}",
        )
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()
    return Probe(
        reachable=True,
        seconds=_elapsed(began),
        detail="the endpoint accepts a TCP connection; no IEC frame was sent",
    )


async def validate_target(
    store: ProjectStore,
    bench_name: str,
    target_name: str,
    *,
    probe: bool = False,
    timeout: float = PROBE_DEFAULT_SECONDS,
) -> TargetReport:
    """The report of a target of a stored bench. The probe runs only when asked for.

    Raises ServiceError: `not_found` for an unknown bench or target, `invalid_argument`
    for a probe timeout out of range, and `invalid_definition` when the bench does not
    check.
    """
    if not PROBE_MIN_SECONDS <= timeout <= PROBE_MAX_SECONDS:
        raise ServiceError(
            "invalid_argument",
            f"the probe timeout is {timeout:g} s: it is "
            f"{PROBE_MIN_SECONDS:g} to {PROBE_MAX_SECONDS:g} seconds",
        )
    try:
        bench = store.bench(bench_name)
    except Missing as error:
        raise ServiceError("not_found", str(error)) from error
    except DefinitionError as error:
        raise ServiceError("invalid_definition", str(error)) from error
    except StoreError as error:
        raise ServiceError("storage_error", str(error)) from error
    target = next((t for t in bench.external_targets if t.name == target_name), None)
    if target is None:
        raise ServiceError(
            "not_found",
            f"the bench {bench_name!r} has no external target {target_name!r}",
        )
    report = report_of(target)
    if probe:
        report.probe = await probe_tcp(target.endpoint, timeout)
    return report


def _elapsed(began: float) -> float:
    return asyncio.get_running_loop().time() - began
