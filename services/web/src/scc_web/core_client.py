"""Web's calls to core-api, on the internal network, with the correlation ID (scc_common.http)."""

from typing import Any

import httpx

from scc_common.http import internal_client


class CoreError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"core {status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class CoreClient:
    def __init__(
        self, base_url: str, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.base_url = base_url
        self.transport = transport

    async def _call(self, method: str, path: str, **kwargs) -> Any:
        async with internal_client(self.base_url, transport=self.transport) as client:
            response = await client.request(method, path, **kwargs)
        if response.status_code >= 400:
            detail = {}
            try:
                body = response.json().get("detail")
                detail = body if isinstance(body, dict) else {}
            except ValueError:
                pass
            raise CoreError(
                response.status_code,
                detail.get("code", "error"),
                detail.get("message", ""),
            )
        return response.json()

    async def create_job(
        self, user_id: int, input_text: str, idempotency_key: str
    ) -> dict:
        return await self._call(
            "POST",
            "/jobs",
            json={"user_id": user_id, "input_text": input_text},
            headers={"Idempotency-Key": idempotency_key},
        )

    async def get_job(self, job_id: str) -> dict:
        return await self._call("GET", f"/jobs/{job_id}")

    async def job_events(self, job_id: str) -> list[dict]:
        return await self._call("GET", f"/jobs/{job_id}/events")

    async def confirm(self, job_id: str, claim: dict) -> dict:
        return await self._call("POST", f"/jobs/{job_id}/confirm", json=claim)

    async def list_jobs(self, user_id: int, limit: int = 50) -> list[dict]:
        return await self._call(
            "GET", "/jobs", params={"user_id": user_id, "limit": limit}
        )
