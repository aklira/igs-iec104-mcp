"""Milestone 1 of IMPLEMENTATION.md §13, run from Python (task S3).

A station runtime and a client runtime run as local processes. The sequence: the client
connects, starts the data transfer, and interrogates the station. The station's point is
read from the interrogation. The point is then updated through the station's API, and
the client observes the update as a spontaneous ASDU. Both runtimes stop.

Each step reads the client's events after a cursor taken before the step
(IMPLEMENTATION.md §8), so an earlier answer cannot satisfy a later step."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from igs_iec104_mcp.runtime.api import ControlClient
from igs_iec104_mcp.runtime.asdu import single_points
from igs_iec104_mcp.runtime.process import (
    LocalRuntime,
    StopResult,
    read_control_token,
    write_control_token,
)

T = TypeVar("T")

POINT_ADDRESS: int = 1
INITIAL_VALUE: bool = False
UPDATED_VALUE: bool = True
# The points of the station: one single-point status, at its initial value.
POINTS: dict[str, Any] = {
    "schema_version": 1,
    "points": [{"address": POINT_ADDRESS, "kind": "single", "value": INITIAL_VALUE}],
}
# The longest a step may wait for the runtime.
PATIENCE: float = 10.0
POLL: float = 0.05
# The cause of transmission of the interrogation answers, and of the termination (IEC
# 60870-5-104 §8).
CAUSE_INTERROGATED: int = 20
CAUSE_SPONTANEOUS: int = 3
CAUSE_TERMINATION: int = 10


class MilestoneError(Exception):
    """A step of the milestone did not reach its state."""


@dataclass(frozen=True)
class Observation:
    """What a step observed, and whether it matched the expectation."""

    step: str
    passed: bool
    detail: str


@dataclass
class MilestoneReport:
    """The outcome of one run of the milestone."""

    observations: list[Observation] = field(default_factory=list)
    event_kinds: list[str] = field(default_factory=list)
    step_seconds: dict[str, float] = field(default_factory=dict)
    station_stop: StopResult | None = None
    client_stop: StopResult | None = None

    @property
    def passed(self) -> bool:
        stopped = all(
            stop is not None and stop.exit_code == 0 and not stop.killed
            for stop in (self.station_stop, self.client_stop)
        )
        return (
            stopped
            and bool(self.observations)
            and all(o.passed for o in self.observations)
        )


async def run_milestone_one(binary: Path, directory: Path) -> MilestoneReport:
    """Runs the milestone with the runtime at `binary`; the points go in `directory`."""
    points = directory / "points.json"
    points.write_text(json.dumps(POINTS), encoding="utf-8")
    token_file = write_control_token(directory)
    token = read_control_token(token_file)
    report = MilestoneReport()
    station = LocalRuntime(
        binary,
        [
            "--listen",
            "127.0.0.1:0",
            "--points",
            str(points),
            "--control",
            "127.0.0.1:0",
            "--control-token-file",
            str(token_file),
        ],
    )
    client = LocalRuntime(binary, [])
    station_api: ControlClient | None = None
    client_api: ControlClient | None = None
    try:
        began = time.monotonic()
        station_addresses = await station.start()
        if station_addresses.station is None:
            raise MilestoneError("the station did not report its listen address")
        client = LocalRuntime(
            binary,
            [
                "--connect",
                station_addresses.station,
                "--control",
                "127.0.0.1:0",
                "--control-token-file",
                str(token_file),
            ],
        )
        client_addresses = await client.start()
        report.step_seconds["start"] = time.monotonic() - began
        station_api = ControlClient(station_addresses.control, token)
        client_api = ControlClient(client_addresses.control, token)

        await _run_steps(report, station_api, client_api)
    except MilestoneError as error:
        report.observations.append(
            Observation("sequence", passed=False, detail=str(error))
        )
    finally:
        if client_api is not None:
            report.event_kinds = await _event_kinds(client_api)
        for api in (station_api, client_api):
            if api is not None:
                await api.aclose()
        report.client_stop = await client.stop()
        report.station_stop = await station.stop()
    return report


async def _run_steps(
    report: MilestoneReport, station_api: ControlClient, client_api: ControlClient
) -> None:
    await _timed(report, "connect", _connect(client_api))
    await _timed(report, "start_transfer", _start_transfer(client_api))
    await _timed(report, "interrogate", _interrogate(report, client_api))
    await _timed(report, "update_point", _update(report, station_api, client_api))


async def _timed(report: MilestoneReport, step: str, action: Awaitable[None]) -> None:
    began = time.monotonic()
    try:
        await action
    finally:
        report.step_seconds[step] = time.monotonic() - began


async def _connect(client_api: ControlClient) -> None:
    async def connected() -> dict[str, Any] | None:
        body = await client_api.ready()
        return body if body.get("connection") == "up" else None

    await _until(connected, "the client to connect")


async def _start_transfer(client_api: ControlClient) -> None:
    await client_api.start_transfer()

    async def started() -> dict[str, Any] | None:
        body = await client_api.events()
        return body if _any_event(body, _is_started) else None

    await _until(started, "the transfer to start")


def _is_started(event: dict[str, Any]) -> bool:
    return event.get("kind") == "transfer" and event.get("state") == "started"


async def _interrogate(report: MilestoneReport, client_api: ControlClient) -> None:
    cursor = (await client_api.events())["last_seq"]
    await client_api.general_interrogation()
    events = await _events_after(
        client_api,
        cursor,
        lambda e: e.get("kind") == "asdu" and e["asdu"]["cause"] == CAUSE_TERMINATION,
        "the interrogation to end",
    )
    values = [
        point.on
        for event in events
        if _is_asdu(event, CAUSE_INTERROGATED)
        for point in single_points(event["asdu"])
        if point.address == POINT_ADDRESS
    ]
    passed = bool(values) and values[-1] == INITIAL_VALUE
    report.observations.append(
        Observation(
            "interrogate",
            passed=passed,
            detail=f"point {POINT_ADDRESS} answered {values[-1:] or 'nothing'}, "
            f"expected {INITIAL_VALUE}",
        )
    )


async def _update(
    report: MilestoneReport, station_api: ControlClient, client_api: ControlClient
) -> None:
    cursor = (await client_api.events())["last_seq"]
    answer = await station_api.update_point(POINT_ADDRESS, UPDATED_VALUE)
    stored = (await station_api.point(POINT_ADDRESS))["value"]
    report.observations.append(
        Observation(
            "update_point",
            passed=answer.get("value") == UPDATED_VALUE and stored == UPDATED_VALUE,
            detail=f"the station stores {stored}, expected {UPDATED_VALUE}",
        )
    )
    events = await _events_after(
        client_api,
        cursor,
        lambda e: (
            _is_asdu(e, CAUSE_SPONTANEOUS)
            and any(
                point.address == POINT_ADDRESS and point.on == UPDATED_VALUE
                for point in single_points(e["asdu"])
            )
        ),
        "the client to observe the update",
    )
    observed = [e for e in events if _is_asdu(e, CAUSE_SPONTANEOUS)]
    report.observations.append(
        Observation(
            "observe_update",
            passed=bool(observed),
            detail=f"{len(observed)} spontaneous ASDU(s) after the update",
        )
    )


def _is_asdu(event: dict[str, Any], cause: int) -> bool:
    return event.get("kind") == "asdu" and event["asdu"]["cause"] == cause


def _any_event(body: dict[str, Any], matches: Callable[[dict[str, Any]], bool]) -> bool:
    return any(matches(event) for event in body.get("events", []))


async def _until[T](check: Callable[[], Awaitable[T | None]], what: str) -> T:
    try:
        async with asyncio.timeout(PATIENCE):
            while True:
                value = await check()
                if value is not None:
                    return value
                await asyncio.sleep(POLL)
    except TimeoutError as error:
        raise MilestoneError(f"timed out waiting for {what}") from error


async def _events_after(
    client_api: ControlClient,
    cursor: int,
    matches: Callable[[dict[str, Any]], bool],
    what: str,
) -> list[dict[str, Any]]:
    """The events after `cursor`, once one of them matches."""

    async def found() -> list[dict[str, Any]] | None:
        body = await client_api.events(after=cursor)
        events = body.get("events", [])
        return events if any(matches(event) for event in events) else None

    return await _until(found, what)


async def _event_kinds(client_api: ControlClient) -> list[str]:
    """The kinds of the events the client recorded, in the order they first appeared."""
    kinds: list[str] = []
    for event in (await client_api.events()).get("events", []):
        if event["kind"] not in kinds:
            kinds.append(event["kind"])
    return kinds
