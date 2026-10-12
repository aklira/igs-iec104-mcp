"""The fault-injecting proxy of the benches (IMPLEMENTATION.md §6, §8; task V3a).

The proxy is a standalone program in `docker/`, so it is loaded from its file here.
The tests run it on the loopback with ports the system chooses (port 0), and read
those ports after each listener is bound: no fixed port is assumed. Each mode is checked
on real connections: bytes pass unchanged, latency delays them, an interrupt closes the
open connections and refuses the new ones, and the control API refuses a request without
the token.
"""

import asyncio
import importlib.util
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "faultproxy", ROOT / "docker" / "faultproxy.py"
)
assert _SPEC is not None and _SPEC.loader is not None
faultproxy: ModuleType = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(faultproxy)

TIMEOUT = 5.0


async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Writes back every byte it reads, until the peer closes."""
    try:
        while chunk := await reader.read(1024):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


@asynccontextmanager
async def proxy_and_echo() -> AsyncIterator[tuple[int, int, str]]:
    """An echo target, and the proxy in front of it. Yields the proxy's forwarding port,
    its control port, and the token."""
    token = secrets.token_urlsafe(32)
    target = await asyncio.start_server(echo, "127.0.0.1", 0)
    target_port = target.sockets[0].getsockname()[1]
    faults = faultproxy.Faults()
    forwarding, controlling = await faultproxy.start(
        ("127.0.0.1", 0),
        ("127.0.0.1", target_port),
        ("127.0.0.1", 0),
        token,
        faults,
    )
    forward_port = forwarding.sockets[0].getsockname()[1]
    control_port = controlling.sockets[0].getsockname()[1]
    try:
        yield forward_port, control_port, token
    finally:
        for server in (forwarding, controlling, target):
            server.close()
            await server.wait_closed()


async def roundtrip(port: int, data: bytes) -> bytes:
    """Sends data through the proxy and reads back as many bytes as were sent."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(data)
        await writer.drain()
        return await asyncio.wait_for(reader.readexactly(len(data)), TIMEOUT)
    finally:
        writer.close()


async def read_to_end(port: int) -> bytes:
    """Sends a byte and reads until the proxy closes the connection. A close with a byte
    still unread reaches the client as a reset: that is a close too, and gives b""."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(b"x")
        await writer.drain()
        return await asyncio.wait_for(reader.read(), TIMEOUT)
    except ConnectionResetError:
        return b""
    finally:
        writer.close()


def control(port: int, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_bytes_pass_through_the_proxy_unchanged() -> None:
    async with proxy_and_echo() as (forward_port, _, _):
        assert await roundtrip(forward_port, b"IEC 104 frame") == b"IEC 104 frame"


async def test_the_control_api_refuses_a_request_without_the_token() -> None:
    async with proxy_and_echo() as (_, control_port, token):
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{control_port}"
        ) as anonymous:
            assert (await anonymous.get("/v1/fault")).status_code == 401
        wrong = {"Authorization": f"Bearer {secrets.token_urlsafe(32)}"}
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{control_port}", headers=wrong
        ) as stranger:
            assert (await stranger.get("/v1/fault")).status_code == 401
        async with control(control_port, token) as client:
            state = (await client.get("/v1/fault")).json()
    assert state["mode"] == "none"
    assert state["latency_ms"] == 0


async def test_latency_delays_the_bytes_until_the_mode_is_cleared() -> None:
    async with proxy_and_echo() as (forward_port, control_port, token):
        async with control(control_port, token) as client:
            set_latency = await client.put(
                "/v1/fault", json={"mode": "latency", "latency_ms": 200}
            )
            assert set_latency.status_code == 200
            started = time.monotonic()
            assert await roundtrip(forward_port, b"slow") == b"slow"
            delayed = time.monotonic() - started
            assert (
                await client.put("/v1/fault", json={"mode": "none"})
            ).status_code == 200
        assert delayed >= 0.2, "each direction of the exchange waited for the latency"
        started = time.monotonic()
        assert await roundtrip(forward_port, b"fast") == b"fast"
        assert time.monotonic() - started < delayed


async def test_an_interrupt_closes_the_open_connections_and_refuses_new_ones() -> None:
    async with proxy_and_echo() as (forward_port, control_port, token):
        reader, writer = await asyncio.open_connection("127.0.0.1", forward_port)
        try:
            writer.write(b"before")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(6), TIMEOUT) == b"before"
            async with control(control_port, token) as client:
                cut = await client.put("/v1/fault", json={"mode": "interrupt"})
                assert cut.status_code == 200
                assert cut.json()["interrupted"] == 1
            assert await asyncio.wait_for(reader.read(), TIMEOUT) == b"", (
                "the open connection was closed by the proxy"
            )
        finally:
            writer.close()
        assert await read_to_end(forward_port) == b"", "a new connection is refused"
        async with control(control_port, token) as client:
            state = (await client.get("/v1/fault")).json()
        assert state["mode"] == "interrupt"
        assert state["refused"] >= 1
        assert state["open"] == 0


async def test_clearing_the_interrupt_admits_connections_again() -> None:
    async with proxy_and_echo() as (forward_port, control_port, token):
        async with control(control_port, token) as client:
            await client.put("/v1/fault", json={"mode": "interrupt"})
            await client.put("/v1/fault", json={"mode": "none"})
        assert await roundtrip(forward_port, b"again") == b"again"


async def test_a_request_the_modes_do_not_allow_is_refused() -> None:
    async with (
        proxy_and_echo() as (_, control_port, token),
        control(control_port, token) as client,
    ):
        refused = [
            {"mode": "drop"},
            {"mode": "latency", "latency_ms": 0},
            {"mode": "latency", "latency_ms": 60_001},
            {"mode": "none", "latency_ms": 5},
            {"mode": 3},
            {"mode": "none", "latency_ms": True},
            ["not", "an", "object"],
        ]
        for body in refused:
            response = await client.put("/v1/fault", json=body)
            assert response.status_code == 400, body
        not_json = await client.put(
            "/v1/fault",
            content=b"{mode",
            headers={"Content-Type": "application/json"},
        )
        assert not_json.status_code == 400
        too_large = await client.put(
            "/v1/fault", content=b"x" * 5000, headers={"Content-Type": "text/plain"}
        )
        assert too_large.status_code == 400
        assert (await client.get("/v1/other")).status_code == 404
        assert (await client.post("/v1/fault")).status_code == 405


def test_the_token_file_is_read_without_its_line_end(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("abc-token\n", encoding="utf-8")
    assert faultproxy.read_token(str(path)) == "abc-token"
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        faultproxy.read_token(str(path))


async def test_the_bench_client_of_the_proxy_sets_and_reads_its_mode() -> None:
    from igs_iec104_mcp.runtime.api import ControlApiError
    from igs_iec104_mcp.runtime.faults import FaultClient

    async with proxy_and_echo() as (forward_port, control_port, token):
        client = FaultClient(f"127.0.0.1:{control_port}", token)
        try:
            set_latency = await client.set("latency", 30)
            assert set_latency["mode"] == "latency"
            assert (await client.state())["latency_ms"] == 30
            await client.set("none")
            assert (await client.state())["mode"] == "none"
            with pytest.raises(ControlApiError) as refused:
                await client.set("drop")
            assert refused.value.status == 400
        finally:
            await client.aclose()
        assert await roundtrip(forward_port, b"after") == b"after"
        stranger = FaultClient(f"127.0.0.1:{control_port}", secrets.token_urlsafe(32))
        try:
            with pytest.raises(ControlApiError) as unauthorized:
                await stranger.state()
            assert unauthorized.value.status == 401
        finally:
            await stranger.aclose()
