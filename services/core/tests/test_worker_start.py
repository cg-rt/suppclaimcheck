"""P3.13 (RB-04 rehearsal, 2026-11-06): a worker that starts while the database is down must exit, so
Docker restarts it until the database answers. It used to hang instead: the connection pool, opened
before the config was read, kept a reconnect task running, so the process never finished exiting.
It stayed up and unhealthy, with no traceback, and no check ran until someone restarted it."""

import os
import subprocess
import sys


def test_the_worker_exits_when_the_database_is_down_at_start(tmp_path):
    password = tmp_path / "password"
    password.write_text("not used", encoding="utf-8")
    env = {
        **os.environ,
        "SCC_DB_HOST": "127.0.0.1",
        "SCC_DB_PORT": "1",  # nothing listens here: the connection is refused at once
        "SCC_DB_USER": "scc_core_app",
        "SCC_DB_PASSWORD_FILE": str(password),
        "SCC_GATEWAY_URL": "http://127.0.0.1:1",
    }
    try:
        done = subprocess.run(
            [sys.executable, "-m", "scc_core.worker"],
            check=False,  # the exit code is the point of the test
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "the worker hung instead of exiting with the database down"
        ) from None
    assert done.returncode != 0
    assert "psycopg" in done.stderr  # OperationalError, or its ConnectionTimeout
