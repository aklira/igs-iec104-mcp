"""A fault-injecting TCP proxy of a test bench (IMPLEMENTATION.md §6, §8; task V3).

It forwards each connection it accepts to one target, byte for byte, as the relay does.
Its control API can add latency to the bytes it forwards, or interrupt: then it closes
every connection it carries, and refuses the next ones, until the mode is cleared. It
changes no byte, drops none, and holds no protocol logic. Only the standard library is
used.

The control API is HTTP/1.1 on its own port. A request needs the bench's token as a
bearer header; the token is read from a file, never from the arguments:

    GET  /v1/fault   the mode, the latency, and the counts
    PUT  /v1/fault   {"mode": "none" | "latency" | "interrupt", "latency_ms": N}

    faultproxy.py --listen 0.0.0.0:2404 --target 172.20.0.5:2404 \
        --control 0.0.0.0:8080 --control-token-file /run/igs/token
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import ipaddress
import json
import sys
from typing import Any

COPY_CHUNK = 65_536
MODES = ("none", "latency", "interrupt")
MAX_LATENCY_MS = 60_000
MAX_BODY = 4_096
HEADER_LIMIT = 8_192
REQUEST_SECONDS = 10.0


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


class Faults:
    """The mode of the proxy, and the connections it carries. An interrupt closes the
    connections at once; a refused connection is counted, not forwarded."""

    def __init__(self) -> None:
        self.mode = "none"
        self.latency_ms = 0
        self.open = 0
        self.interrupted = 0
        self.refused = 0
        self._writers: set[asyncio.StreamWriter] = set()

    def set(self, mode: str, latency_ms: int) -> None:
        if mode not in MODES:
            raise ValueError(f"the mode must be one of {', '.join(MODES)}")
        if mode == "latency" and not 1 <= latency_ms <= MAX_LATENCY_MS:
            raise ValueError(f"the latency must be 1 to {MAX_LATENCY_MS} milliseconds")
        if mode != "latency" and latency_ms != 0:
            raise ValueError("a latency belongs to the latency mode only")
        self.mode = mode
        self.latency_ms = latency_ms
        if mode == "interrupt":
            # Each connection is tracked by its two sockets, so it is counted once.
            self.interrupted += len(self._writers) // 2
            for writer in list(self._writers):
                writer.close()

    def admits(self) -> bool:
        return self.mode != "interrupt"

    async def delay(self) -> None:
        if self.mode == "latency":
            await asyncio.sleep(self.latency_ms / 1000)

    def track(self, *writers: asyncio.StreamWriter) -> None:
        self._writers.update(writers)
        self.open += 1

    def untrack(self, *writers: asyncio.StreamWriter) -> None:
        self._writers.difference_update(writers)
        self.open -= 1

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "latency_ms": self.latency_ms,
            "open": self.open,
            "interrupted": self.interrupted,
            "refused": self.refused,
        }


async def copy(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, faults: Faults
) -> None:
    """Copies bytes from one side to the other, with the current delay, until the
    sending side closes."""
    try:
        while chunk := await reader.read(COPY_CHUNK):
            await faults.delay()
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
    faults: Faults,
) -> None:
    """Relays one connection, unless the mode refuses it. When either side ends, both
    are closed."""
    if not faults.admits():
        faults.refused += 1
        client_writer.close()
        return
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(*target)
    except OSError:
        client_writer.close()
        return
    if not faults.admits():
        faults.refused += 1
        client_writer.close()
        upstream_writer.close()
        return
    faults.track(client_writer, upstream_writer)
    try:
        await asyncio.gather(
            copy(client_reader, upstream_writer, faults),
            copy(upstream_reader, client_writer, faults),
        )
    finally:
        faults.untrack(client_writer, upstream_writer)


def _response(status: str, body: dict[str, Any]) -> bytes:
    text = json.dumps(body, sort_keys=True).encode("utf-8")
    head = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(text)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode("latin-1") + text


async def _request(
    reader: asyncio.StreamReader, token: str
) -> tuple[str, str, dict[str, str], bytes]:
    """Reads one request: its method, path, headers, and body. Raises ValueError for a
    request that is not HTTP/1.1 or is larger than the limits."""
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > HEADER_LIMIT:
        raise ValueError("the request head is too large")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[2] != "HTTP/1.1":
        raise ValueError("the request line is not HTTP/1.1")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError as error:
        raise ValueError("the content length is not a number") from error
    if not 0 <= length <= MAX_BODY:
        raise ValueError(f"the body must be at most {MAX_BODY} bytes")
    body = await reader.readexactly(length) if length else b""
    return parts[0], parts[1], headers, body


async def handle_control(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    faults: Faults,
    token: str,
) -> None:
    """Answers one control request, then closes the connection."""
    try:
        method, path, headers, body = await asyncio.wait_for(
            _request(reader, token), REQUEST_SECONDS
        )
        supplied = headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            status, answer = "401 Unauthorized", {"error": "a token is required"}
        elif path != "/v1/fault":
            status, answer = "404 Not Found", {"error": "no such path"}
        elif method == "GET":
            status, answer = "200 OK", faults.describe()
        elif method == "PUT":
            status, answer = _put(faults, body)
        else:
            status, answer = "405 Method Not Allowed", {"error": "GET or PUT only"}
    except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        status, answer = "400 Bad Request", {"error": "the request is not valid"}
    except TimeoutError:
        status, answer = "408 Request Timeout", {"error": "the request is too slow"}
    writer.write(_response(status, answer))
    with contextlib.suppress(ConnectionError, OSError):
        await writer.drain()
    writer.close()


def _put(faults: Faults, body: bytes) -> tuple[str, dict[str, Any]]:
    try:
        request = json.loads(body)
    except ValueError:
        return "400 Bad Request", {"error": "the body is not JSON"}
    if not isinstance(request, dict):
        return "400 Bad Request", {"error": "the body is not an object"}
    mode = request.get("mode")
    latency_ms = request.get("latency_ms", 0)
    if (
        not isinstance(mode, str)
        or not isinstance(latency_ms, int)
        or isinstance(latency_ms, bool)
    ):
        return "400 Bad Request", {"error": "mode is a text, latency_ms a number"}
    try:
        faults.set(mode, latency_ms)
    except ValueError as error:
        return "400 Bad Request", {"error": str(error)}
    return "200 OK", faults.describe()


async def start(
    listen: tuple[str, int],
    target: tuple[str, int],
    control: tuple[str, int],
    token: str,
    faults: Faults,
) -> tuple[asyncio.Server, asyncio.Server]:
    """Opens the forwarding and the control listeners. Each is bound when this returns,
    so a caller can read the port a listener got."""
    forwarding = await asyncio.start_server(
        lambda reader, writer: forward(reader, writer, target, faults), *listen
    )
    controlling = await asyncio.start_server(
        lambda reader, writer: handle_control(reader, writer, faults, token), *control
    )
    return forwarding, controlling


def read_token(path: str) -> str:
    with open(path, encoding="utf-8") as file:
        token = file.read().rstrip("\n")
    if not token:
        raise ValueError(f"the token file {path} is empty")
    return token


async def serve(
    listen: tuple[str, int],
    target: tuple[str, int],
    control: tuple[str, int],
    token: str,
) -> None:
    forwarding, controlling = await start(listen, target, control, token, Faults())
    async with forwarding, controlling:
        await asyncio.gather(forwarding.serve_forever(), controlling.serve_forever())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Relay TCP with injected faults.")
    parser.add_argument("--listen", required=True, type=address)
    parser.add_argument("--target", required=True, type=address)
    parser.add_argument("--control", required=True, type=address)
    parser.add_argument("--control-token-file", required=True)
    arguments = parser.parse_args(argv)
    try:
        token = read_token(arguments.control_token_file)
        asyncio.run(serve(arguments.listen, arguments.target, arguments.control, token))
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        print(f"faultproxy: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
