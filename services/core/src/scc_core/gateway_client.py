"""Core's calls to the gateway, the only module that reaches outside services (D-011).

Core never holds an API key and never calls the internet: it asks the gateway, which prices, caps,
records and retries every outside call.
"""

from typing import Any
from uuid import UUID

import httpx

from scc_common.http import internal_client

# LLM calls can take a while; the gateway applies its own timeout and retries inside this one.
TIMEOUT = httpx.Timeout(180.0, connect=3.0)


class GatewayError(Exception):
    """The gateway refused or failed. `code` is the gateway's error code, such as budget_cap_reached."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"gateway {status} {code}: {message}")
        self.status = status
        self.code = code


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        lane: str = "live",
    ):
        self.base_url = base_url
        self.transport = transport
        # The budget lane every LLM call is charged to: "live" for users, "eval" for the eval runner (D-021).
        self.lane = lane

    async def _post(self, path: str, body: dict) -> dict:
        async with internal_client(self.base_url, TIMEOUT, self.transport) as client:
            response = await client.post(path, json=body)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", {})
            except ValueError:
                detail = {}
            if not isinstance(detail, dict):
                detail = {"code": "error", "message": str(detail)}
            raise GatewayError(
                response.status_code,
                detail.get("code", "error"),
                detail.get("message", ""),
            )
        return response.json()

    async def structured(
        self,
        *,
        stage: str,
        job_id: UUID,
        user_id: int,
        settings: dict[str, Any],
        model: str,
        instructions: str,
        data: str,
        schema: dict,
    ) -> dict:
        """Asks the LLM for JSON matching `schema`. `data` is untrusted and is delimited by the gateway."""
        return await self._post(
            "/llm/structured",
            {
                "stage": stage,
                "job_id": str(job_id),
                "user_id": user_id,
                "lane": self.lane,
                "model": model,
                "max_tokens": settings["max_tokens"],
                "effort": settings["effort"],
                "thinking": settings["thinking"],
                "instructions": instructions,
                "data": data,
                "schema": schema,
            },
        )

    async def search(self, query: str, max_results: int) -> dict:
        return await self._post(
            "/literature/search", {"query": query, "max_results": max_results}
        )

    async def fetch(self, pmids: list[str], full_texts: int) -> dict:
        return await self._post(
            "/literature/fetch", {"pmids": pmids, "full_texts": full_texts}
        )

    async def notify(self, event: str, text: str, code: str | None = None) -> dict:
        """Asks the gateway to alert the owner. Never send user text here: codes and IDs only."""
        return await self._post("/notify", {"event": event, "text": text, "code": code})
