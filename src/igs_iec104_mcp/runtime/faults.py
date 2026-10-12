"""The control client of a fault proxy (IMPLEMENTATION.md §8; task V3b).

The proxy's control API is the one of `docker/faultproxy.py`: `GET /v1/fault` reads its
mode, and `PUT /v1/fault` sets it. Every request carries the bench's token as a bearer
credential, as the runtime's control API requires (D-009). The client is not a runtime
client: a proxy has no readiness and no events.
"""

from __future__ import annotations

from typing import Any

import httpx

from igs_iec104_mcp.runtime.api import ControlApiError

FAULT_PATH = "/v1/fault"
FAULT_MODES = ("none", "latency", "interrupt")


class FaultClient:
    """Calls the control API of one fault proxy."""

    def __init__(self, control: str, token: str) -> None:
        self._http = httpx.AsyncClient(
            base_url=f"http://{control}",
            timeout=10.0,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def state(self) -> dict[str, Any]:
        """The mode, the latency, and the counts of the proxy."""
        return await self._call("GET", FAULT_PATH)

    async def set(self, mode: str, latency_ms: int = 0) -> dict[str, Any]:
        """Sets the mode. The latency belongs to the latency mode only, and the proxy
        refuses any other combination."""
        body: dict[str, Any] = {"mode": mode}
        if mode == "latency":
            body["latency_ms"] = latency_ms
        return await self._call("PUT", FAULT_PATH, json=body)

    async def _call(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self._http.request(method, path, json=json)
        body: Any = response.json()
        if response.status_code >= 400:
            raise ControlApiError(method, path, response.status_code, body)
        if not isinstance(body, dict):
            raise ControlApiError(method, path, response.status_code, body)
        return body
