"""Runtime processes run as local processes (task S3): start them, learn their
addresses, stop them.

A runtime prints its bound addresses once on standard output, as one JSON line, and logs
to standard error. The controller reads the first line to learn the control address,
which the runtime chose itself (IMPLEMENTATION.md §6), and keeps the last lines of the
log for diagnostics."""

from __future__ import annotations

import asyncio
import json
import signal
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# The longest a runtime may take to print its addresses.
READY_TIMEOUT: Final = 15.0
# The longest a runtime may take to stop after SIGTERM. A station drains its connections
# for at most five seconds (task U1), so this allows for that and a margin.
STOP_TIMEOUT: Final = 20.0
# The number of log lines kept for diagnostics.
LOG_LINES: Final = 200


class RuntimeProcessError(Exception):
    """A runtime process did not start, or did not print its addresses."""


@dataclass(frozen=True)
class Addresses:
    """The addresses a runtime reports when it starts."""

    control: str
    station: str | None


@dataclass(frozen=True)
class StopResult:
    """How a runtime stopped."""

    exit_code: int | None
    seconds: float
    killed: bool


class LocalRuntime:
    """One runtime process, started with the given arguments."""

    def __init__(self, binary: Path, arguments: list[str]) -> None:
        self._binary = binary
        self._arguments = arguments
        self._process: asyncio.subprocess.Process | None = None
        self._log: deque[str] = deque(maxlen=LOG_LINES)
        self._drain: asyncio.Task[None] | None = None

    async def start(self) -> Addresses:
        """Starts the process and returns the addresses it reports."""
        self._process = await asyncio.create_subprocess_exec(
            str(self._binary),
            *self._arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._drain = asyncio.create_task(self._drain_log(self._process))
        assert self._process.stdout is not None
        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(), READY_TIMEOUT
            )
        except TimeoutError as error:
            await self.stop()
            raise RuntimeProcessError(
                "the runtime did not print its addresses"
            ) from error
        if not line:
            await self.stop()
            raise RuntimeProcessError(f"the runtime exited at start: {self.log_tail()}")
        return _addresses(line)

    async def stop(self) -> StopResult:
        """Stops the process with SIGTERM, and kills it if it does not stop in time."""
        process = self._process
        if process is None:
            # Never started: nothing to stop, and no exit code.
            return StopResult(exit_code=None, seconds=0.0, killed=False)
        began = time.monotonic()
        killed = False
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), STOP_TIMEOUT)
            except TimeoutError:
                killed = True
                process.kill()
                await process.wait()
        if self._drain is not None:
            await self._drain
        return StopResult(
            exit_code=process.returncode,
            seconds=time.monotonic() - began,
            killed=killed,
        )

    def log_tail(self) -> str:
        """The last lines of the log, for a message."""
        return " | ".join(self._log)

    async def _drain_log(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        async for raw in process.stderr:
            self._log.append(raw.decode("utf-8", errors="replace").rstrip())


def _addresses(line: bytes) -> Addresses:
    try:
        data = json.loads(line)
    except json.JSONDecodeError as error:
        raise RuntimeProcessError(
            f"the runtime printed no address line: {line!r}"
        ) from error
    control = data.get("control_address")
    station = data.get("station_address")
    if not isinstance(control, str) or not (
        station is None or isinstance(station, str)
    ):
        raise RuntimeProcessError(f"the address line has an unexpected form: {line!r}")
    return Addresses(control=control, station=station)
