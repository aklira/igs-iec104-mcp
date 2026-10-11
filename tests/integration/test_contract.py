"""The runtime answers as the contract says (task C1), on each of its routes.

A station and a client run as local processes. Each response has a documented
status, and a body that matches the schema of that response.
"""

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from igs_iec104_mcp.runtime.milestone1 import POINTS
from igs_iec104_mcp.runtime.process import (
    LocalRuntime,
    read_control_token,
    write_control_token,
)

PATIENCE = 10.0


def check(
    contract: Any,
    template: str,
    method: str,
    status: int,
    body: Any,
    *,
    schema: Callable[[Any], dict[str, Any]] | None = None,
) -> None:
    """The status is documented for the route, and the body matches its schema."""
    assert contract.documents_status(template, method, status), (
        f"{method} {template} answered {status}, which the contract does not document"
    )
    wanted = (
        schema(contract)
        if schema
        else contract.response_schema(template, method, status)
    )
    errors = list(contract.validator(wanted).iter_errors(body))
    assert not errors, (
        f"{method} {template} {status}: {errors[0].message} in {json.dumps(body)}"
    )


async def call(
    http: httpx.AsyncClient, method: str, path: str, body: Any = None
) -> tuple[int, Any, httpx.Headers]:
    response = await http.request(method, path, json=body)
    return response.status_code, response.json(), response.headers


async def wait_for(what: str, reached: Callable[[], Any]) -> None:
    deadline = time.monotonic() + PATIENCE
    while not await reached():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


async def test_the_station_and_the_client_answer_as_the_contract_says(
    runtime_binary: Path, contract: Any, tmp_path: Path
) -> None:
    token_file = write_control_token(tmp_path)
    token = read_control_token(token_file)
    points = tmp_path / "points.json"
    points.write_text(json.dumps(POINTS), encoding="utf-8")
    station = LocalRuntime(
        runtime_binary,
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
    client = LocalRuntime(runtime_binary, [])
    try:
        station_addresses = await station.start()
        assert station_addresses.station is not None
        client = LocalRuntime(
            runtime_binary,
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
        auth = {"Authorization": f"Bearer {token}"}
        async with (
            httpx.AsyncClient(
                base_url=f"http://{station_addresses.control}", headers=auth
            ) as s,
            httpx.AsyncClient(
                base_url=f"http://{client_addresses.control}", headers=auth
            ) as c,
            httpx.AsyncClient(
                base_url=f"http://{station_addresses.control}"
            ) as anonymous,
        ):
            await check_station(contract, s, anonymous)
            await check_client(contract, c)
    finally:
        await client.stop()
        await station.stop()


async def check_station(
    contract: Any, s: httpx.AsyncClient, anonymous: httpx.AsyncClient
) -> None:
    status, body, _ = await call(s, "GET", "/v1/ready")
    check(contract, "/v1/ready", "get", status, body)
    assert (
        body["role"] == "station"
        and body["contract"] == contract.api["info"]["version"]
    )

    status, body, _ = await call(s, "GET", "/v1/points")
    check(contract, "/v1/points", "get", status, body)
    assert len(body["points"]) == 1

    status, body, _ = await call(s, "GET", "/v1/points/1")
    check(contract, "/v1/points/{address}", "get", status, body)

    status, body, _ = await call(s, "POST", "/v1/points/1", {"value": True})
    check(contract, "/v1/points/{address}", "post", status, body)
    assert body["queued"] is True

    status, body, _ = await call(s, "POST", "/v1/points/1", {"value": 3})
    check(contract, "/v1/points/{address}", "post", status, body)
    assert status == 400

    status, body, _ = await call(s, "GET", "/v1/points/99")
    check(contract, "/v1/points/{address}", "get", status, body)
    assert status == 404

    status, body, _ = await call(s, "POST", "/v1/transfer/start")
    check(contract, "/v1/transfer/start", "post", status, body)
    assert status == 409, "a station does not start a transfer"

    status, body, _ = await call(s, "GET", "/v1/events")
    check(contract, "/v1/events", "get", status, body)
    assert status == 409, "a station keeps no client events"

    status, body, headers = await call(anonymous, "GET", "/v1/ready")
    check(contract, "/v1/ready", "get", status, body)
    assert status == 401
    assert headers.get("www-authenticate") == "Bearer"


async def check_client(contract: Any, c: httpx.AsyncClient) -> None:
    async def connected() -> bool:
        _, body, _ = await call(c, "GET", "/v1/ready")
        return bool(body.get("connection") == "up")

    await wait_for("the client to connect", connected)
    status, body, _ = await call(c, "GET", "/v1/ready")
    check(contract, "/v1/ready", "get", status, body)
    assert body["role"] == "client"

    status, body, _ = await call(c, "POST", "/v1/transfer/start")
    check(contract, "/v1/transfer/start", "post", status, body)
    assert status == 202

    status, body, _ = await call(c, "POST", "/v1/interrogation/general")
    check(contract, "/v1/interrogation/general", "post", status, body)
    assert status == 202

    async def interrogated() -> bool:
        _, body, _ = await call(c, "GET", "/v1/events")
        return any(
            event["kind"] == "asdu" and event["asdu"]["cause"] == 10
            for event in body["events"]
        )

    await wait_for("the interrogation to end", interrogated)
    status, body, _ = await call(c, "GET", "/v1/events")
    check(contract, "/v1/events", "get", status, body)
    kinds = {event["kind"] for event in body["events"]}
    assert {"connected", "transfer", "asdu"} <= kinds

    status, body, _ = await call(c, "POST", "/v1/points/1", {"value": True})
    check(contract, "/v1/points/{address}", "post", status, body)
    assert status == 409, "a client serves no points"

    status, body, _ = await call(
        c, "POST", "/v1/commands/single", {"address": 0, "on": True, "select": True}
    )
    check(contract, "/v1/commands/single", "post", status, body)
    assert status == 400, "address 0 is not a point"

    status, body, _ = await call(
        c, "POST", "/v1/commands/single", {"address": 1, "on": True, "select": True}
    )
    check(contract, "/v1/commands/single", "post", status, body)
    assert status == 202
    request = body["request"]

    async def ended() -> bool:
        _, view, _ = await call(c, "GET", f"/v1/requests/{request}")
        return view["outcome"] is not None

    await wait_for("the command to end", ended)
    status, view, _ = await call(c, "GET", f"/v1/requests/{request}")
    check(contract, "/v1/requests/{id}", "get", status, view)
    # The milestone station refuses every command: cause 47, as a negative confirmation.
    assert (
        view["outcome"] == "refused"
        and view["cause"] == 47
        and view["negative"] is True
    )

    status, body, _ = await call(c, "POST", f"/v1/requests/{request}/cancel")
    check(contract, "/v1/requests/{id}/cancel", "post", status, body)
    assert status == 409, "a request that ended cannot be cancelled"

    status, body, _ = await call(c, "GET", "/v1/requests/999999")
    check(contract, "/v1/requests/{id}", "get", status, body)
    assert status == 404

    status, body, _ = await call(c, "GET", "/v1/image/points")
    check(contract, "/v1/image/points", "get", status, body)
    assert any(point["address"] == 1 for point in body["points"])
