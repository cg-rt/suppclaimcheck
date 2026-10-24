"""core-api: the ops endpoints answer (P1.01). Real dependency checks run in the dev stack."""

from fastapi.testclient import TestClient

from scc_core.api import build_app


async def ok() -> None:
    return None


def test_health_version_and_deps():
    client = TestClient(build_app({"database": ok}))
    assert client.get("/health").status_code == 200
    assert client.get("/version").json()["service"] == "core-api"
    assert client.get("/health/deps").json() == {
        "status": "ok",
        "checks": {"database": "ok"},
    }
