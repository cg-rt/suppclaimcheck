"""Record and replay core's calls to the gateway, for the free eval tier (P2.03, D-043).

The transport sits between core and the gateway, so one recording covers both the LLM and the literature.
A request is keyed by its path and a hash of its body, without the fields that change on every run
(job ID, user ID, budget lane). So:
- a rubric change sends the same requests: the replay recomputes verdicts and scores for free;
- a prompt, model, query or retrieval change sends different requests: the replay misses, the case is
  marked stale, and only a new live run (which costs money) can refresh the recording.

Alerts (`/notify`) are answered here and never forwarded: an eval failure must not page the owner.
"""

import gzip
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import httpx

_VOLATILE = {"job_id", "user_id", "lane"}


class StaleRecording(Exception):
    """The pipeline sent a request that was not recorded: something upstream of the rubric changed."""


def request_key(request: httpx.Request) -> str:
    body = json.loads(request.content) if request.content else {}
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k not in _VOLATILE}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return f"{request.method} {request.url.path} {digest[:24]}"


def _body_name(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:16] + ".json.gz"


class EvalTransport(httpx.AsyncBaseTransport):
    """mode "live": forward to the gateway (recording if `store` is set); mode "replay": answer from `store`.

    Either way it adds up each job's LLM cost and call count from the gateway's replies.
    """

    def __init__(
        self,
        mode: str,
        store: Path | None = None,
        inner: httpx.AsyncBaseTransport | None = None,
    ):
        """`inner` replaces the real HTTP transport in live mode; for tests only."""
        if mode not in {"live", "replay"}:
            raise ValueError(mode)
        self.mode = mode
        self.store = store
        self.inner = (inner or httpx.AsyncHTTPTransport()) if mode == "live" else None
        self.recorded: dict[str, list[dict]] = defaultdict(list)
        self.bodies: dict[str, bytes] = {}
        self.replayed: dict[str, int] = defaultdict(int)
        self.cost_eur: dict[str, float] = defaultdict(float)
        self.llm_calls: dict[str, int] = defaultdict(int)
        if mode == "replay":
            if store is None or not (store / "index.json").exists():
                raise FileNotFoundError(
                    f"no recording in {store}: run a live eval first"
                )
            self.recorded.update(json.loads((store / "index.json").read_text("utf-8")))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/notify":
            return httpx.Response(
                200, json={"sent": False, "reason": "eval"}, request=request
            )
        key = request_key(request)
        if self.mode == "live":
            response = await self.inner.handle_async_request(request)
            status, body = response.status_code, await response.aread()
            if self.store is not None:
                name = _body_name(body)
                self.bodies[name] = body
                self.recorded[key].append({"status": status, "file": name})
        else:
            entries = self.recorded.get(key)
            if not entries:
                raise StaleRecording(key)
            # Identical requests get their recorded answers in order; the last one repeats.
            entry = entries[min(self.replayed[key], len(entries) - 1)]
            self.replayed[key] += 1
            status = entry["status"]
            body = gzip.decompress((self.store / entry["file"]).read_bytes())
        self._tally(request, status, body)
        return httpx.Response(
            status,
            content=body,
            headers={"content-type": "application/json"},
            request=request,
        )

    def _tally(self, request: httpx.Request, status: int, body: bytes) -> None:
        if request.url.path != "/llm/structured" or status != 200:
            return
        job_id = json.loads(request.content).get("job_id", "")
        self.cost_eur[job_id] += json.loads(body).get("cost_eur", 0.0)
        self.llm_calls[job_id] += 1

    def save(self) -> None:
        """Writes the recording, replacing the old one, so requests no longer made drop out.

        Bodies are stored once each, gzipped (article full texts are large) and named by their hash.
        """
        if self.mode != "live" or self.store is None:
            return
        self.store.mkdir(parents=True, exist_ok=True)
        for old in self.store.glob("*.json.gz"):
            old.unlink()
        for name, body in self.bodies.items():
            # mtime=0 keeps the bytes identical for identical bodies, so git sees no change.
            (self.store / name).write_bytes(gzip.compress(body, mtime=0))
        (self.store / "index.json").write_text(
            json.dumps(self.recorded, indent=1, sort_keys=True) + "\n",
            "utf-8",
            newline="\n",
        )

    async def aclose(self) -> None:
        """Does nothing: httpx closes the transport with each client, and core opens one client per call."""

    async def shutdown(self) -> None:
        if self.inner is not None:
            await self.inner.aclose()
