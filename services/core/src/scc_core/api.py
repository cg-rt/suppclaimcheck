"""core-api: jobs and confirmation (P1.05). In M1 it only has the ops endpoints (P1.01)."""

import os

from fastapi import FastAPI

from scc_common.app import Check, create_app, http_check
from scc_common.db import conninfo_from_env, db_check


def build_app(checks: dict[str, Check] | None = None) -> FastAPI:
    if checks is None:
        checks = {
            "database": db_check(conninfo_from_env()),
            "gateway": http_check(os.environ["SCC_GATEWAY_URL"]),
        }
    return create_app("core-api", "SuppClaimCheck core API", checks)
