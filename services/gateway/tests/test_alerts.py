"""P1.14: Telegram alerts. Quiet events, repeats held back, transient retries, the /notify endpoint, and the
budget-cap alert. Telegram is replaced by a mock transport; no live calls."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from scc_gateway import budget, config
from scc_gateway.alerts import AlertFailed, Alerts, AlertsNotConfigured
from scc_gateway.main import Deps, build_app
from tests.helpers import run_async

CFG = config.load()


class FakeTelegram:
    def __init__(self, *statuses: int):
        self.statuses = list(statuses) or [200]
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(status, json={"ok": status == 200})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def alerts(telegram: FakeTelegram, clock=None, token="t0ken", chat="42") -> Alerts:
    cfg = {**CFG, "alerts": {**CFG["alerts"], "backoff_base_s": 0, "backoff_max_s": 0}}
    return Alerts(
        cfg, token, chat, transport=telegram.transport(), clock=clock or Clock()
    )


def test_sends_the_event_code_and_text_to_the_chat():
    telegram = FakeTelegram()
    assert (
        run_async(
            alerts(telegram).send(
                "job_failed", "Job 7 stopped.", "provider_unavailable"
            )
        )
        == "sent"
    )
    request = telegram.requests[0]
    assert request.url.path == "/bott0ken/sendMessage"
    body = json.loads(request.content)
    assert body["chat_id"] == "42"
    assert (
        body["text"]
        == "SuppClaimCheck · job_failed · provider_unavailable\nJob 7 stopped."
    )


def test_quiet_codes_send_nothing():
    telegram = FakeTelegram()
    assert (
        run_async(alerts(telegram).send("job_failed", "Job 7 stopped.", "no_claim"))
        == "quiet"
    )
    assert telegram.requests == []


def test_a_repeat_inside_the_window_is_held_back_then_sent_again():
    telegram, clock = FakeTelegram(), Clock()
    a = alerts(telegram, clock)
    assert (
        run_async(a.send("job_failed", "Job 1 stopped.", "provider_unavailable"))
        == "sent"
    )
    assert (
        run_async(a.send("job_failed", "Job 2 stopped.", "provider_unavailable"))
        == "deduplicated"
    )
    assert run_async(a.send("job_failed", "Job 3 stopped.", "invalid_output")) == "sent"
    clock.now += CFG["alerts"]["dedupe_minutes"] * 60 + 1
    assert (
        run_async(a.send("job_failed", "Job 4 stopped.", "provider_unavailable"))
        == "sent"
    )
    assert len(telegram.requests) == 3


def test_transient_errors_are_retried_and_rejections_are_not():
    flaky = FakeTelegram(502, 429, 200)
    assert run_async(alerts(flaky).send("test", "hello")) == "sent"
    assert len(flaky.requests) == 3

    refused = FakeTelegram(401)
    with pytest.raises(AlertFailed) as caught:
        run_async(alerts(refused).send("test", "hello"))
    assert len(refused.requests) == 1
    assert "t0ken" not in str(caught.value)


def test_missing_token_or_chat_is_reported():
    with pytest.raises(AlertsNotConfigured):
        run_async(alerts(FakeTelegram(), token=None).send("test", "hello"))


def _client(telegram: FakeTelegram, configured: bool = True) -> TestClient:
    a = alerts(telegram) if configured else alerts(telegram, token=None)
    deps = Deps(pool=None, provider=None, literature=None, config=CFG, alerts=a)
    return TestClient(build_app({}, deps=deps))


def test_notify_endpoint_outcomes():
    telegram = FakeTelegram()
    client = _client(telegram)
    assert client.post("/notify", json={"event": "test", "text": "hello"}).json() == {
        "outcome": "sent"
    }
    quiet = client.post(
        "/notify", json={"event": "job_failed", "code": "no_claim", "text": "x"}
    )
    assert quiet.json() == {"outcome": "quiet"}
    assert (
        client.post(
            "/notify", json={"event": "job_failed", "code": "Bad Code!", "text": "x"}
        ).status_code
        == 422
    )
    assert (
        client.post("/notify", json={"event": "anything", "text": "x"}).status_code
        == 422
    )
    assert (
        _client(FakeTelegram(), configured=False)
        .post("/notify", json={"event": "test", "text": "x"})
        .json()["detail"]["code"]
        == "alerts_not_configured"
    )
    assert (
        _client(FakeTelegram(400))
        .post("/notify", json={"event": "test", "text": "x"})
        .json()["detail"]["code"]
        == "alert_failed"
    )


def test_budget_cap_refusal_alerts_once(monkeypatch):
    telegram = FakeTelegram()

    async def refuse(**kwargs):
        raise budget.CapReached("live", 15.01, 15.0)

    monkeypatch.setattr("scc_gateway.main.structured_call", refuse)
    client = _client(telegram)
    body = {
        "stage": "extract", "model": "claude-haiku-5-5", "max_tokens": 100, "effort": "low",
        "thinking": "disabled", "instructions": "x", "data": "y", "schema": {"type": "object"},
    }  # fmt: skip
    for _ in range(2):
        assert client.post("/llm/structured", json=body).status_code == 402
    assert len(telegram.requests) == 1
    assert (
        "live lane reached its cap" in json.loads(telegram.requests[0].content)["text"]
    )
