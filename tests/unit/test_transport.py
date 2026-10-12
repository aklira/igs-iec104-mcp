"""The HTTP transport: its token, its listener, and what a request must carry
(IMPLEMENTATION.md §9; task M4).

The application is driven in process, through the SDK's ASGI app and an HTTP client on
the ASGI transport: no port is opened, so the tests do not depend on the host. The
token file rules are checked on real files. A token never appears in an error.
"""

import json
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from igs_iec104_mcp.server import create_server, main
from igs_iec104_mcp.transport import (
    PATH,
    HttpSettings,
    StaticToken,
    TokenError,
    read_token,
    server_options,
    transport_security,
    validate_token,
)

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
ACCEPT = {"Accept": "application/json, text/event-stream"}


def token() -> str:
    return secrets.token_urlsafe(32)


def write_token(path: Path, value: str, mode: int = 0o600) -> Path:
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def project_in(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


def payload(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC message of a response: JSON, or one server-sent event."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        line = next(
            line for line in response.text.splitlines() if line.startswith("data: ")
        )
        message: dict[str, Any] = json.loads(line[len("data: ") :])
        return message
    message = json.loads(response.text)
    return message


@asynccontextmanager
async def client_of(
    tmp_path: Path, settings: HttpSettings
) -> AsyncIterator[httpx.AsyncClient]:
    """A client of the server's HTTP application, with its lifespan running."""
    server = create_server(project_in(tmp_path), http=settings)
    app = server.streamable_http_app(
        streamable_http_path=PATH,
        transport_security=transport_security(settings),
        host=settings.host,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=f"http://127.0.0.1:{settings.port}"
        ) as client:
            yield client


# The token file


def test_a_token_file_that_only_its_owner_reads_is_read(tmp_path: Path) -> None:
    value = token()
    path = write_token(tmp_path / "token", value)
    assert read_token(path, project=project_in(tmp_path)) == value


def test_a_token_file_readable_by_others_is_refused_without_its_token(
    tmp_path: Path,
) -> None:
    value = token()
    path = write_token(tmp_path / "token", value, mode=0o644)
    with pytest.raises(TokenError, match="readable by others") as error:
        read_token(path, project=project_in(tmp_path))
    assert value not in str(error.value)


def test_a_token_file_that_is_a_symbolic_link_is_refused(tmp_path: Path) -> None:
    target = write_token(tmp_path / "real", token())
    link = tmp_path / "link"
    os.symlink(target, link)
    with pytest.raises(TokenError, match="symbolic link"):
        read_token(link, project=project_in(tmp_path))


def test_a_token_file_inside_the_project_is_refused(tmp_path: Path) -> None:
    project = project_in(tmp_path)
    path = write_token(project / "token", token())
    with pytest.raises(TokenError, match="inside the project directory"):
        read_token(path, project=project)


def test_a_missing_token_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(TokenError, match="cannot be read"):
        read_token(tmp_path / "absent", project=project_in(tmp_path))


def test_a_short_or_spaced_token_is_refused_without_showing_it() -> None:
    with pytest.raises(TokenError, match="must be 32 to 256") as error:
        validate_token("short")
    assert "short" not in str(error.value)
    with pytest.raises(TokenError):
        validate_token("a" * 31 + " ")


# The listener


def test_the_listener_is_on_the_loopback_only() -> None:
    with pytest.raises(TokenError, match="loopback only"):
        HttpSettings(host="0.0.0.0", port=8765, token=token())
    with pytest.raises(TokenError, match="loopback only"):
        HttpSettings(host="192.0.2.7", port=8765, token=token())


def test_a_port_is_a_port() -> None:
    with pytest.raises(TokenError, match="not a port"):
        HttpSettings(host="127.0.0.1", port=0, token=token())


def test_the_settings_do_not_show_their_token() -> None:
    value = token()
    assert value not in repr(HttpSettings(host="127.0.0.1", port=8765, token=value))


def test_stdio_needs_no_token_option() -> None:
    assert server_options(None) == {}


async def test_the_token_verifier_accepts_its_token_only() -> None:
    value = token()
    verifier = StaticToken(value)
    accepted = await verifier.verify_token(value)
    assert accepted is not None and accepted.client_id == "igs-iec104-mcp"
    assert await verifier.verify_token(token()) is None


# The HTTP application


async def test_a_request_without_the_token_is_refused(tmp_path: Path) -> None:
    settings = HttpSettings(host="127.0.0.1", port=8765, token=token())
    async with client_of(tmp_path, settings) as client:
        response = await client.post(PATH, json=INIT, headers=ACCEPT)
    assert response.status_code == 401


async def test_a_request_with_another_token_is_refused(tmp_path: Path) -> None:
    settings = HttpSettings(host="127.0.0.1", port=8765, token=token())
    headers = {**ACCEPT, "Authorization": f"Bearer {token()}"}
    async with client_of(tmp_path, settings) as client:
        response = await client.post(PATH, json=INIT, headers=headers)
    assert response.status_code == 401


async def test_a_host_name_that_is_not_the_listener_is_refused(tmp_path: Path) -> None:
    value = token()
    settings = HttpSettings(host="127.0.0.1", port=8765, token=value)
    headers = {
        **ACCEPT,
        "Authorization": f"Bearer {value}",
        "Host": "rebinding.example",
    }
    async with client_of(tmp_path, settings) as client:
        response = await client.post(PATH, json=INIT, headers=headers)
    assert response.status_code == 421


async def test_an_origin_that_is_not_the_listener_is_refused(tmp_path: Path) -> None:
    value = token()
    settings = HttpSettings(host="127.0.0.1", port=8765, token=value)
    headers = {
        **ACCEPT,
        "Authorization": f"Bearer {value}",
        "Origin": "http://evil.example",
    }
    async with client_of(tmp_path, settings) as client:
        response = await client.post(PATH, json=INIT, headers=headers)
    assert response.status_code == 403


async def test_a_client_with_the_token_lists_the_tools(tmp_path: Path) -> None:
    value = token()
    settings = HttpSettings(host="127.0.0.1", port=8765, token=value)
    headers = {**ACCEPT, "Authorization": f"Bearer {value}"}
    async with client_of(tmp_path, settings) as client:
        initialized = await client.post(PATH, json=INIT, headers=headers)
        assert initialized.status_code == 200
        assert payload(initialized)["result"]["serverInfo"]["name"] == "igs-iec104-mcp"
        session = {**headers, "mcp-session-id": initialized.headers["mcp-session-id"]}
        await client.post(
            PATH,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session,
        )
        listed = await client.post(
            PATH,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=session,
        )
    names = {tool["name"] for tool in payload(listed)["result"]["tools"]}
    assert len(names) == 19
    assert {"bench_validate", "scenario_run", "run_report", "run_prune"} <= names


# The command line


def test_the_http_transport_needs_its_port_and_its_token_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--project", str(project_in(tmp_path)), "--transport", "http"])
    assert error.value.code == 2


def test_a_remote_listener_is_refused_before_the_server_starts(tmp_path: Path) -> None:
    token_file = write_token(tmp_path / "token", token())
    status = main(
        [
            "--project",
            str(project_in(tmp_path)),
            "--transport",
            "http",
            "--http-host",
            "0.0.0.0",
            "--http-port",
            "8765",
            "--http-token-file",
            str(token_file),
        ]
    )
    assert status == 2
