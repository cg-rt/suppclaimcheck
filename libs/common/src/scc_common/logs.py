"""JSON logs to stdout, one object per line, each carrying the correlation ID (AGENTS.md section 7)."""

import json
import logging
import sys
from datetime import UTC, datetime

from scc_common import correlation
from scc_common.version import version_info

# Attributes every LogRecord has; anything else on a record came from `extra=` and is logged as a field.
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


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
            "correlation_id": correlation.current(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and key not in entry:
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


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
