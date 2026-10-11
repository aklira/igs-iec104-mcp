"""A TCP relay: every connection it accepts is forwarded to one target, byte for byte
(IMPLEMENTATION.md §6).

A runtime keeps its own network, which is internal to the bench. The relay is the only
container of the bench that the host can reach. It holds no protocol logic and no token:
it copies bytes in both directions, and nothing else. Only the standard library is used.

    relay.py --listen 0.0.0.0:8080 --target 172.20.0.5:8080
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import sys

COPY_CHUNK = 65_536


def address(text: str) -> tuple[str, int]:
    """The host and the port of an `address:port` text. Raises ValueError otherwise."""
    host, separator, port_text = text.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise ValueError(f"{text!r} is not an address and a port, such as 0.0.0.0:8080")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError(f"the port {port} is out of range")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError as error:
        raise ValueError(f"{host!r} is not an IP address") from error
    return host.strip("[]"), port


async def copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copies bytes from one side to the other until the sending side closes."""
    try:
        while chunk := await reader.read(COPY_CHUNK):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def forward(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target: tuple[str, int],
) -> None:
    """Relays one connection; when either side ends, both are closed."""
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(*target)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(
        copy(client_reader, upstream_writer),
        copy(upstream_reader, client_writer),
    )


async def serve(listen: tuple[str, int], target: tuple[str, int]) -> None:
    """Accepts connections on `listen` and relays each one to `target`."""
    server = await asyncio.start_server(
        lambda reader, writer: forward(reader, writer, target), *listen
    )
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Relay TCP connections to a target.")
    parser.add_argument("--listen", required=True, type=address)
    parser.add_argument("--target", required=True, type=address)
    arguments = parser.parse_args(argv)
    try:
        asyncio.run(serve(arguments.listen, arguments.target))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
