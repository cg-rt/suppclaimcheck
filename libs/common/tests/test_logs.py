"""JSON log lines (P1.01): one object per line, with service, version and correlation ID."""

import io
import json
import logging

from scc_common import correlation
from scc_common.logs import JsonFormatter


def log_line(bound: str | None = "log-cid-0001", **extra) -> dict:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("svc"))
    logger = logging.getLogger("test.logs")
    logger.handlers[:] = [handler]
    logger.propagate = False
    if bound:
        with correlation.bind(bound):
            logger.warning("hello %s", "world", extra=extra)
    else:
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


def test_procrastinate_job_lines_take_the_id_from_the_job_arguments():
    # Procrastinate logs a job's start and end outside the task, where no ID is bound yet.
    job = {"task_kwargs": {"job_id": "j1", "correlation_id": "job-cid-0001"}}
    assert log_line(bound=None, job=job)["correlation_id"] == "job-cid-0001"


def test_a_bound_id_wins_and_odd_job_fields_are_ignored():
    job = {"task_kwargs": {"correlation_id": "job-cid-0001"}}
    assert log_line(job=job)["correlation_id"] == "log-cid-0001"
    assert log_line(bound=None, job="not a dict")["correlation_id"] is None
    assert (
        log_line(bound=None, job={"task_kwargs": {"correlation_id": 7}})[
            "correlation_id"
        ]
        is None
    )
