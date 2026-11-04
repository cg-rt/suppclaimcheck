"""Core's calls to the gateway, the only module that reaches outside services (D-011).

Core never holds an API key and never calls the internet: it asks the gateway, which prices, caps,
records and retries every outside call.
"""

import hashlib
import json
from typing import Any
from uuid import UUID

import httpx

from scc_common.http import internal_client

# LLM calls can take a while; the gateway applies its own timeout and retries inside this one.
TIMEOUT = httpx.Timeout(180.0, connect=3.0)
# The budget check before a new job is quick, and a user waits for it.
STATUS_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


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

    async def _post(self, path: str, body: dict, headers: dict | None = None) -> dict:
        async with internal_client(self.base_url, TIMEOUT, self.transport) as client:
            response = await client.post(path, json=body, headers=headers)
        return self._reply(response)

    def _reply(self, response: httpx.Response) -> dict:
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
        attempt: int = 1,
    ) -> dict:
        """Asks the LLM for JSON matching `schema`. `data` is untrusted and is delimited by the gateway.

        The Idempotency-Key names this exact request of this job (P3.05, D-053): if the job step runs
        again (a worker restart, an admin retry), the gateway returns the stored answer instead of
        charging a new call. `attempt` keeps a deliberate second try (after an unusable answer) apart.
        """
        body = {
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
        }
        return await self._post(
            "/llm/structured",
            body,
            headers={"Idempotency-Key": idempotency_key(body, attempt)},
        )

    async def budget_status(self, user_id: int | None = None) -> dict:
        """Whether a new check may start, for this account (P3.04): the gateway is the budget's keeper."""
        params = {"user_id": user_id} if user_id else None
        async with internal_client(
            self.base_url, STATUS_TIMEOUT, self.transport
        ) as client:
            response = await client.get("/budget/status", params=params)
        return self._reply(response)

    async def search(self, query: str, max_results: int) -> dict:
        return await self._post(
            "/literature/search", {"query": query, "max_results": max_results}
        )

    async def spell(self, term: str) -> dict:
        """PubMed's spelling suggestion for a supplement name: {"term", "suggestion"}, None when none."""
        return await self._post("/literature/spell", {"term": term})

    async def fetch(self, pmids: list[str], full_texts: int) -> dict:
        return await self._post(
            "/literature/fetch", {"pmids": pmids, "full_texts": full_texts}
        )

    async def notify(self, event: str, text: str, code: str | None = None) -> dict:
        """Asks the gateway to alert the owner. Never send user text here: codes and IDs only."""
        return await self._post("/notify", {"event": event, "text": text, "code": code})


def idempotency_key(body: dict, attempt: int) -> str:
    """The job, the stage, the attempt and a hash of everything else the request says."""
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return f"{body['job_id']}:{body['stage']}:{attempt}:{digest[:32]}"
