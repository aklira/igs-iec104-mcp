"""The relay of a bench (docker/relay.py, D-018): it reads an address, and it forwards
the bytes of each connection to its target, both ways, without reading them."""

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_relay() -> ModuleType:
    """The relay is a script of the image, not a package: it is loaded from its file."""
    spec = importlib.util.spec_from_file_location("relay", ROOT / "docker" / "relay.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relay = load_relay()


@pytest.mark.parametrize(
    "text,expected",
    [("0.0.0.0:8080", ("0.0.0.0", 8080)), ("172.20.0.5:2404", ("172.20.0.5", 2404))],
)
def test_an_address_and_a_port_are_read(text: str, expected: tuple[str, int]) -> None:
    assert relay.address(text) == expected


@pytest.mark.parametrize(
    "text", ["8080", "host:8080", "0.0.0.0", "0.0.0.0:0", "0.0.0.0:70000", "1.2.3.4:x"]
)
def test_a_bad_address_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        relay.address(text)


@pytest.fixture
async def echo_target() -> AsyncIterator[tuple[str, int]]:
    """A target that sends back what it receives, as a control API would answer."""

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(1024):
                writer.write(data.upper())
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield ("127.0.0.1", port)
    server.close()
    await server.wait_closed()


async def test_bytes_go_to_the_target_and_back_through_the_relay(
    echo_target: tuple[str, int],
) -> None:
    listen = ("127.0.0.1", 0)
    server = await asyncio.start_server(
        lambda reader, writer: relay.forward(reader, writer, echo_target),
        *listen,
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping over the relay")
        await writer.drain()
        assert (
            await asyncio.wait_for(reader.readexactly(19), 5) == b"PING OVER THE RELAY"
        )
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def test_a_connection_to_a_target_that_is_gone_is_closed() -> None:
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    dead_port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    server = await asyncio.start_server(
        lambda reader, writer: relay.forward(reader, writer, ("127.0.0.1", dead_port)),
        "127.0.0.1",
        0,
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await asyncio.wait_for(reader.read(1), 5) == b""
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
