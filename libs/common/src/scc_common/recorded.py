"""Recorded HTTP fixtures (AGENTS.md section 7: no live calls in CI).

`RecordingTransport` saves every response it passes through; `ReplayTransport` serves them back and
fails loudly on any request that wasn't recorded. Requests are keyed by method and URL, with the
`api_key` parameter removed so keys never reach a fixture file.
"""

import hashlib
import json
from pathlib import Path

import httpx

_SECRET_PARAMS = {"api_key"}


def request_key(request: httpx.Request) -> str:
    url = (
        request.url.copy_remove_param("api_key")
        if "api_key" in request.url.params
        else request.url
    )
    return f"{request.method} {url}"


def _file_name(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, folder: Path):
        self.folder = folder
        self.inner = httpx.AsyncHTTPTransport()
        folder.mkdir(parents=True, exist_ok=True)
        self.index_path = folder / "index.json"
        self.index = (
            json.loads(self.index_path.read_text("utf-8"))
            if self.index_path.exists()
            else {}
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        body = await response.aread()
        key = request_key(request)
        name = _file_name(key)
        (self.folder / name).write_bytes(body)
        self.index[key] = {
            "file": name,
            "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
        }
        self.index_path.write_text(
            json.dumps(self.index, indent=1, sort_keys=True) + "\n", "utf-8"
        )
        # The body is already decoded, so the encoding headers no longer describe it.
        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower()
            not in {"content-encoding", "content-length", "transfer-encoding"}
        }
        return httpx.Response(
            response.status_code, headers=headers, content=body, request=request
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, folder: Path):
        self.folder = folder
        self.index = json.loads((folder / "index.json").read_text("utf-8"))
        self.requests: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        self.requests.append(key)
        entry = self.index.get(key)
        if entry is None:
            raise AssertionError(f"no recorded response for {key}")
        content = (self.folder / entry["file"]).read_bytes()
        return httpx.Response(
            entry["status"],
            headers={"content-type": entry["content_type"]},
            content=content,
            request=request,
        )
