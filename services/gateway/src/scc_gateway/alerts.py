"""Alerts to the owner's phone through a Telegram bot (P1.14, D-024).

Callers send an event and a short text; the gateway drops events marked quiet in config and holds back
a repeat of the same event inside the dedupe window, so an outage sends one message, not hundreds.
Texts never carry what a user submitted: only codes, counts and IDs.
"""

import logging
import time
from collections.abc import Callable

import httpx

from scc_gateway.retry import Transient, with_retries

log = logging.getLogger("scc.alerts")
API = "https://api.telegram.org"


class AlertsNotConfigured(Exception):
    pass


class AlertFailed(Exception):
    pass


class Alerts:
    def __init__(
        self,
        config: dict,
        bot_token: str | None,
        chat_id: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config["alerts"]
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.transport = transport
        self.clock = clock
        self._last_sent: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _quiet(self, event: str, code: str | None) -> bool:
        return event == "job_failed" and code in self.config["quiet_job_failure_codes"]

    def _repeat(self, key: str) -> bool:
        last = self._last_sent.get(key)
        return (
            last is not None
            and self.clock() - last < self.config["dedupe_minutes"] * 60
        )

    async def send(self, event: str, text: str, code: str | None = None) -> str:
        """Returns "sent", "quiet" or "deduplicated". Raises when it should have sent and couldn't."""
        if self._quiet(event, code):
            return "quiet"
        key = f"{event}:{code or ''}"
        if self._repeat(key):
            log.info(
                "alert held back as a repeat", extra={"event": event, "code": code}
            )
            return "deduplicated"
        if not self.configured:
            raise AlertsNotConfigured("no Telegram bot token or chat ID")
        message = f"SuppClaimCheck · {event}{f' · {code}' if code else ''}\n{text}"
        await with_retries(
            lambda: self._post(message),
            attempts=self.config["max_attempts"],
            base_s=self.config["backoff_base_s"],
            max_s=self.config["backoff_max_s"],
            what="telegram",
        )
        self._last_sent[key] = self.clock()
        log.info("alert sent", extra={"event": event, "code": code})
        return "sent"

    async def _post(self, message: str) -> None:
        try:
            async with httpx.AsyncClient(
                base_url=API, timeout=self.config["timeout_s"], transport=self.transport
            ) as client:
                response = await client.post(
                    f"/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": message,
                        "disable_web_page_preview": True,
                    },
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise Transient(type(exc).__name__) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise Transient(f"telegram {response.status_code}")
        if response.status_code >= 400:
            # The token is part of the URL, so the error never includes the request.
            raise AlertFailed(f"telegram {response.status_code}")
