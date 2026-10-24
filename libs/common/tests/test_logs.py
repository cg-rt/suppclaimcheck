"""JSON log lines (P1.01): one object per line, with service, version and correlation ID."""

import io
import json
import logging

from scc_common import correlation
from scc_common.logs import JsonFormatter


def log_line(**extra) -> dict:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("svc"))
    logger = logging.getLogger("test.logs")
    logger.handlers[:] = [handler]
    logger.propagate = False
    with correlation.bind("log-cid-0001"):
        logger.warning("hello %s", "world", extra=extra)
    return json.loads(stream.getvalue())


def test_fields():
    entry = log_line(path="/x", status=200)
    assert entry["msg"] == "hello world"
    assert entry["level"] == "warning"
    assert entry["service"] == "svc"
    assert entry["correlation_id"] == "log-cid-0001"
    assert entry["path"] == "/x" and entry["status"] == 200
    assert entry["version"]


def test_extra_fields_cannot_overwrite_the_standard_ones():
    entry = log_line(service="spoofed")
    assert entry["service"] == "svc"
