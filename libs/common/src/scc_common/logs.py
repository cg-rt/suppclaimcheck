"""JSON logs to stdout, one object per line, each carrying the correlation ID (AGENTS.md section 7).

Secrets that travel in URLs are masked in every line, whichever logger wrote it: httpx logs each
request's full URL, and PubMed's API key is a query parameter while Telegram's bot token is part of
the path (AGENTS.md section 6: never a secret in logs).
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime

from scc_common import correlation
from scc_common.version import version_info

# Attributes every LogRecord has; anything else on a record came from `extra=` and is logged as a field.
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}
MASK = "[redacted]"
_SECRETS = [
    # A secret in a query string: ?api_key=..., &token=...
    (
        re.compile(
            r"([?&](?:api_key|apikey|key|token|access_token|secret)=)[^&\s\"'\\]+",
            re.IGNORECASE,
        ),
        rf"\1{MASK}",
    ),
    # A Telegram bot token in a URL path: /bot123456:ABC-def/sendMessage
    (re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+"), rf"\1{MASK}"),
]


def redact(text: str) -> str:
    """The text with secrets in URLs masked."""
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def _job_correlation_id(record: logging.LogRecord) -> str | None:
    """Procrastinate logs a job's start and end outside the task, so the ID comes from the job's arguments."""
    job = getattr(record, "job", None)
    if isinstance(job, dict) and isinstance(job.get("task_kwargs"), dict):
        value = job["task_kwargs"].get("correlation_id")
        return value if isinstance(value, str) else None
    return None


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        info = version_info(service)
        self.static = {"service": service, "version": info["version"]}

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname.lower(),
            **self.static,
            "correlation_id": correlation.current() or _job_correlation_id(record),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and key not in entry:
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        # Masked after encoding, so a URL in the message, an extra field or a traceback is covered.
        return redact(json.dumps(entry, default=str))


def configure_logging(service: str, level: str = "INFO") -> None:
    """Sends every logger, uvicorn's included, through one JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # The correlation middleware logs each request instead of uvicorn's plain-text access log.
    logging.getLogger("uvicorn.access").disabled = True
