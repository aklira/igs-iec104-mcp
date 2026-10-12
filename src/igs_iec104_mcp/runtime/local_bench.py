"""A bench of local runtime processes: a station, and the client connected to it.

The instance actions drive them (IMPLEMENTATION.md §8; task E5).

The addresses are chosen when the bench is made: the station's listening and control
ports, and the address the client connects to. An instance that starts again gets the
same arguments, so it answers on the addresses it had, as a restarted managed instance
does. The control token is written once, next to the points, and never printed.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from igs_iec104_mcp.runtime.api import ControlClient
from igs_iec104_mcp.runtime.process import (
    LocalRuntime,
    StopResult,
    read_control_token,
    write_control_token,
)

HOST = "127.0.0.1"


def free_port() -> int:
    """A TCP port that is free now. Another process may take it before it is used."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


@dataclass
class _Member:
    """An instance of the bench: its arguments, and its process and control client while
    it runs."""

    arguments: list[str]
    runtime: LocalRuntime | None = None
    api: ControlClient | None = None


class LocalBench:
    """One station and one client, as local processes. The station serves the points of
    the given document; the client connects to the station's listening address."""

    def __init__(
        self,
        binary: Path,
        directory: Path,
        points: dict[str, Any],
        *,
        station: str = "server-1",
        client: str = "client-1",
    ) -> None:
        points_file = directory / "points.json"
        points_file.write_text(json.dumps(points), encoding="utf-8")
        token_file = write_control_token(directory)
        self._binary = binary
        self._token = read_control_token(token_file)
        self._station = station
        self._client = client
        listen = f"{HOST}:{free_port()}"
        self._members: dict[str, _Member] = {
            station: _Member(
                [
                    "--listen",
                    listen,
                    "--points",
                    str(points_file),
                    "--control",
                    f"{HOST}:{free_port()}",
                    "--control-token-file",
                    str(token_file),
                ]
            ),
            client: _Member(
                [
                    "--connect",
                    listen,
                    "--control",
                    f"{HOST}:0",
                    "--control-token-file",
                    str(token_file),
                ]
            ),
        }
        self.stops: list[StopResult] = []

    async def start(self, instance: str) -> None:
        """Starts an instance, and keeps its control client."""
        member = self._member(instance)
        if member.runtime is not None:
            raise RuntimeError(f"{instance} is already running")
        runtime = LocalRuntime(self._binary, member.arguments)
        addresses = await runtime.start()
        member.runtime = runtime
        member.api = ControlClient(addresses.control, self._token)

    async def stop(self, instance: str) -> None:
        """Stops an instance with SIGTERM, and closes its control client."""
        member = self._member(instance)
        if member.runtime is None or member.api is None:
            raise RuntimeError(f"{instance} is not running")
        await member.api.aclose()
        member.api = None
        self.stops.append(await member.runtime.stop())
        member.runtime = None

    def control(self, instance: str) -> ControlClient:
        """The control client of a running instance."""
        api = self._member(instance).api
        if api is None:
            raise RuntimeError(f"{instance} is not running")
        return api

    def clients_of(self, instance: str) -> list[str]:
        """The clients connected to a station."""
        return [self._client] if instance == self._station else []

    def stations_of(self, client: str) -> list[str]:
        """The stations of a client: one, the station of this bench."""
        return [self._station] if client == self._client else []

    async def aclose(self) -> None:
        """Stops every running instance: the client first, then the station."""
        for instance in (self._client, self._station):
            if self._member(instance).runtime is not None:
                await self.stop(instance)

    def _member(self, instance: str) -> _Member:
        member = self._members.get(instance)
        if member is None:
            raise KeyError(f"the bench has no instance named {instance!r}")
        return member
