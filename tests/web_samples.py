"""Sample core-api answers for web's tests, shaped like the real pipeline's output (scc_core.pipeline.run)."""

import json
import uuid

import httpx

CLAIM = {
    "supplement": "creatine",
    "effect": "increases muscle strength with resistance training",
    "population": "older adults",
    "dose": None,
    "duration": None,
}

RESULT = {
    "verdict": "Supported",
    "score": 67,
    "articles_assessed": 1,
    "supporting": 2,
    "contradicting": 0,
    "direction": 1.0,
    "strength": 0.33,
    "rubric_version": "rubric-v0",
    "config_version": "pipeline-v0",
    "articles": [
        {
            "pmid": "31234567",
            "pmcid": "PMC6543210",
            "title": "Creatine supplementation and resistance training in older adults: a randomised trial",
            "authors": "Example A, Sample B",
            "journal": "Nutrients",
            "year": 2019,
            "doi": "10.3390/nu0000000",
            "license": "cc by",
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/31234567/",
            "pmc_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC6543210/",
        }
    ],
    "found_not_assessed": [
        {
            "pmid": "30000001",
            "title": "A review we could not read",
            "year": 2018,
            "skip_reason": "licence_not_allowed",
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/30000001/",
        }
    ],
    "quotes": [
        {
            "passage_id": "PMC6543210:4",
            "pmcid": "PMC6543210",
            "section": "Results",
            "label": "supports",
            "quote": "Upper-body strength increased more in the creatine group than in the placebo group.",
            "reason": "Reports a larger strength gain with creatine.",
        },
        {
            "passage_id": "PMC6543210:9",
            "pmcid": "PMC6543210",
            "section": "Discussion",
            "label": "supports",
            "quote": "Creatine combined with resistance training improved leg press strength.",
            "reason": "States an improvement in strength.",
        },
    ],
    "quotes_dropped": 1,
    "searched": {
        "query": "creatine[tiab] AND (strength[tiab]) AND open access[filter]",
        "query_version": "query-v0",
        "database": "PubMed (open-access filter)",
        "matches": 42,
        "considered": 20,
        "skipped": {"licence_not_allowed": 6, "no_full_text": 3},
    },
    "limitations": [
        "Only open-access articles whose licence allows text mining are read.",
        "This version reads up to 1 article(s) per claim.",
        "This is not medical advice.",
    ],
}


def job(user_id: int, state: str = "done", **overrides) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "state": state,
        "input_type": "text",
        "input_text": "Creatine makes older adults stronger when they lift weights.",
        "extracted_claims": [
            {**CLAIM, "quote": "Creatine makes older adults stronger"}
        ],
        "confirmed_claim": CLAIM
        if state not in {"submitted", "extracting", "awaiting_confirmation"}
        else None,
        "result": RESULT if state == "done" else None,
        "error": None,
        "config_version": "pipeline-v0",
        "created_at": "2026-10-24T10:00:00+00:00",
        "updated_at": "2026-10-24T10:01:00+00:00",
    }
    return {**base, **overrides}


class FakeCore:
    """An in-memory core-api for web's tests: records calls, serves the jobs it holds."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def add(self, j: dict) -> dict:
        self.jobs[j["id"]] = j
        return j

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, body))
        if request.method == "POST" and path == "/jobs":
            new = self.add(
                job(body["user_id"], "submitted", input_text=body["input_text"])
            )
            return httpx.Response(201, json=new)
        if request.method == "GET" and path == "/jobs":
            user_id = int(request.url.params["user_id"])
            rows = [
                {
                    k: j[k]
                    for k in (
                        "id",
                        "state",
                        "input_text",
                        "confirmed_claim",
                        "created_at",
                    )
                }
                | {
                    "verdict": (j["result"] or {}).get("verdict"),
                    "score": (j["result"] or {}).get("score"),
                }
                for j in self.jobs.values()
                if j["user_id"] == user_id
            ]
            return httpx.Response(200, json=rows)
        parts = path.strip("/").split("/")
        j = self.jobs.get(parts[1]) if len(parts) >= 2 else None
        if j is None:
            return httpx.Response(
                404, json={"detail": {"code": "not_found", "message": "No such job."}}
            )
        if len(parts) == 2:
            return httpx.Response(200, json=j)
        if parts[2] == "events":
            return httpx.Response(
                200,
                json=[{"state": "submitted", "detail": None, "at": j["created_at"]}],
            )
        if parts[2] == "confirm":
            j.update(state="searching", confirmed_claim=body)
            return httpx.Response(202, json=j)
        return httpx.Response(404)
