"""Web's calls to the admin endpoints of core-api and the gateway (P3.08-P3.10), on the internal network.

Web may read only its own `auth` schema (D-011), so admin reads core's and the gateway's data through
their read-only `/admin/...` endpoints. The writes are core's retry of a failed job and its record
of a safety review.
"""

from typing import Any

import httpx

from scc_common.http import internal_client

# Admin pages wait for every call they make; a slow module should not hold the page for long.
TIMEOUT = httpx.Timeout(8.0, connect=2.0)


class AdminError(Exception):
    def __init__(self, module: str, status: int, code: str, message: str = ""):
        super().__init__(f"{module} {status} {code}: {message}")
        self.module, self.status, self.code, self.message = (
            module,
            status,
            code,
            message,
        )


class AdminClient:
    def __init__(
        self,
        core_url: str,
        gateway_url: str,
        core_transport: httpx.AsyncBaseTransport | None = None,
        gateway_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.urls = {"core-api": core_url, "gateway": gateway_url}
        self.transports = {"core-api": core_transport, "gateway": gateway_transport}

    async def _call(self, module: str, method: str, path: str, **kwargs) -> Any:
        try:
            async with internal_client(
                self.urls[module], TIMEOUT, self.transports[module]
            ) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise AdminError(module, 0, "unreachable", type(exc).__name__) from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            detail = detail if isinstance(detail, dict) else {}
            raise AdminError(
                module,
                response.status_code,
                detail.get("code", "error"),
                detail.get("message", ""),
            )
        return response.json()

    # core-api
    async def jobs(self, **params) -> dict:
        return await self._call(
            "core-api",
            "GET",
            "/admin/jobs",
            params={k: v for k, v in params.items() if v not in (None, "", [])},
        )

    async def trace(self, job_id: str) -> dict:
        return await self._call("core-api", "GET", f"/admin/jobs/{job_id}/trace")

    async def passages(self, job_id: str, pmcid: str) -> list[dict]:
        return await self._call(
            "core-api", "GET", f"/admin/jobs/{job_id}/passages", params={"pmcid": pmcid}
        )

    async def retry(self, job_id: str, actor_user_id: int) -> dict:
        return await self._call(
            "core-api",
            "POST",
            f"/admin/jobs/{job_id}/retry",
            json={"actor_user_id": actor_user_id},
        )

    async def reviews(self, **params) -> dict:
        return await self._call(
            "core-api",
            "GET",
            "/admin/reviews",
            params={k: v for k, v in params.items() if v is not None},
        )

    async def review(
        self, job_id: str, actor_user_id: int, outcome: str, note: str | None
    ) -> dict:
        return await self._call(
            "core-api",
            "POST",
            f"/admin/jobs/{job_id}/review",
            json={"actor_user_id": actor_user_id, "outcome": outcome, "note": note},
        )

    async def injections(self, **params) -> dict:
        return await self._call(
            "core-api",
            "GET",
            "/admin/injections",
            params={k: v for k, v in params.items() if v is not None},
        )

    async def core_errors(self, limit: int = 50) -> list[dict]:
        return await self._call(
            "core-api", "GET", "/admin/errors", params={"limit": limit}
        )

    async def queue(self) -> dict:
        return await self._call("core-api", "GET", "/admin/queue")

    # gateway
    async def llm_calls(self, **params) -> list[dict]:
        return await self._call(
            "gateway",
            "GET",
            "/admin/llm-calls",
            params={k: v for k, v in params.items() if v is not None},
        )

    async def budget(self) -> dict:
        """Each lane's level this month (P3.04), for the banner on every admin page."""
        return await self._call("gateway", "GET", "/budget/status")

    async def usage(self, days: int = 14) -> dict:
        return await self._call("gateway", "GET", "/admin/usage", params={"days": days})

    # Both modules: config stores (D-048) and the ops endpoints.
    async def config(self, module: str) -> dict:
        return await self._call(module, "GET", "/admin/config")

    async def config_version(self, module: str, version: str) -> dict:
        return await self._call(module, "GET", f"/admin/config/versions/{version}")

    async def health(self, module: str) -> tuple[dict, int]:
        """The module's `/health/deps` body and status code; 503 still has a body worth showing."""
        try:
            async with internal_client(
                self.urls[module], TIMEOUT, self.transports[module]
            ) as client:
                response = await client.get("/health/deps")
            return response.json(), response.status_code
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "unreachable", "error": type(exc).__name__}, 0

    async def version(self, module: str) -> dict:
        return await self._call(module, "GET", "/version")
