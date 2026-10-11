"""The control API of a runtime, from Python (task S3).

The routes are those of the runtime (`runtime/igs-iec104-runtime/src/api.rs`). Until the
contract of task C1 fixes them, this client is a spike: it follows the routes the
runtime has now."""

from __future__ import annotations

from typing import Any

import httpx


class ControlApiError(Exception):
    """The control API answered with an error status."""

    def __init__(self, method: str, path: str, status: int, body: Any) -> None:
        super().__init__(f"{method} {path} answered {status}: {body}")
        self.status = status
        self.body = body


class ControlClient:
    """Calls the control API of one runtime."""

    def __init__(self, control: str) -> None:
        self._http = httpx.AsyncClient(base_url=f"http://{control}", timeout=10.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def ready(self) -> dict[str, Any]:
        return await self._call("GET", "/v1/ready")

    async def start_transfer(self) -> dict[str, Any]:
        return await self._call("POST", "/v1/transfer/start")

    async def general_interrogation(self) -> dict[str, Any]:
        return await self._call("POST", "/v1/interrogation/general")

    async def events(self, after: int = 0) -> dict[str, Any]:
        return await self._call("GET", "/v1/events", params={"after": after})

    async def point(self, address: int) -> dict[str, Any]:
        return await self._call("GET", f"/v1/points/{address}")

    async def update_point(self, address: int, value: Any) -> dict[str, Any]:
        return await self._call("POST", f"/v1/points/{address}", json={"value": value})

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._http.request(method, path, params=params, json=json)
        body: Any = response.json()
        if response.status_code >= 400:
            raise ControlApiError(method, path, response.status_code, body)
        if not isinstance(body, dict):
            raise ControlApiError(method, path, response.status_code, body)
        return body
