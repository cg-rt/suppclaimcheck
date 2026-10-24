"""Correlation IDs (P1.01): accepted, created, returned, bound for jobs, and sent on to the next module."""

import asyncio

import httpx
from fastapi.testclient import TestClient

from scc_common import correlation
from scc_common.app import create_app
from scc_common.http import internal_client


def client() -> TestClient:
    return TestClient(create_app("test", "test"))


def test_incoming_id_is_kept_and_returned():
    response = client().get("/version", headers={"X-Correlation-ID": "abc-12345678"})
    assert response.headers["X-Correlation-ID"] == "abc-12345678"


def test_missing_id_is_created():
    response = client().get("/version")
    assert correlation.is_valid(response.headers["X-Correlation-ID"])


def test_unsafe_id_is_replaced_so_it_cannot_inject_log_text():
    unsafe = 'bad id" level="error'
    response = client().get("/version", headers={"X-Correlation-ID": unsafe})
    returned = response.headers["X-Correlation-ID"]
    assert returned != unsafe
    assert correlation.is_valid(returned)


def test_bind_sets_and_restores_the_id_for_a_job():
    assert correlation.current() is None
    with correlation.bind("job-cid-0001") as value:
        assert value == "job-cid-0001"
        assert correlation.current() == "job-cid-0001"
    assert correlation.current() is None


def test_bind_replaces_an_invalid_id():
    with correlation.bind("x") as value:
        assert value != "x"
        assert correlation.is_valid(value)


def test_internal_client_sends_the_current_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers.get("X-Correlation-ID")
        return httpx.Response(200)

    async def call():
        async with internal_client(
            "http://next", transport=httpx.MockTransport(handler)
        ) as c:
            with correlation.bind("chain-0000001"):
                await c.get("/health")

    asyncio.run(call())
    assert seen["header"] == "chain-0000001"
