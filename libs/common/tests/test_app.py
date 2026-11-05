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


def test_health_answers_head_for_uptime_monitors():
    response = TestClient(create_app("svc", "t")).head("/health")
    assert response.status_code == 200
    assert response.content == b""


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


def test_an_unexpected_error_is_a_coded_500_without_a_trace():
    """P3.05: core and the gateway answer an unhandled error with a code and the correlation ID."""
    from fastapi.testclient import TestClient

    app = create_app("svc", "Test")

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internals: /opt/app/thing.py line 42")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom", headers={"X-Correlation-ID": "err-cid-000001"})
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert (detail["code"], detail["correlation_id"]) == (
        "internal_error",
        "err-cid-000001",
    )
    assert "secret internals" not in response.text and "Traceback" not in response.text
