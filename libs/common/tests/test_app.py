"""The ops endpoints every HTTP module has (P1.01)."""

from fastapi.testclient import TestClient

from scc_common.app import create_app


async def ok() -> None:
    return None


async def broken() -> None:
    raise ConnectionError("down")


def test_health():
    response = TestClient(create_app("svc", "t")).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_reports_the_app_version_and_sha(monkeypatch):
    monkeypatch.setenv("SCC_GIT_SHA", "abc1234")
    body = TestClient(create_app("svc", "t")).get("/version").json()
    assert body["service"] == "svc"
    assert body["git_sha"] == "abc1234"
    assert body["version"].count(".") == 2


def test_deps_ok():
    response = TestClient(create_app("svc", "t", {"database": ok})).get("/health/deps")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}


def test_deps_failure_is_503_and_names_the_dependency():
    response = TestClient(
        create_app("svc", "t", {"database": ok, "gateway": broken})
    ).get("/health/deps")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "ok", "gateway": "failed"}
