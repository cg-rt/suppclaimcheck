"""The web module: public site, admin and login. In M1 it only has the ops endpoints (P1.01)."""

import os

from fastapi import FastAPI

from scc_common.app import Check, create_app, http_check
from scc_common.db import conninfo_from_env, db_check


def build_app(checks: dict[str, Check] | None = None) -> FastAPI:
    if checks is None:
        checks = {
            "database": db_check(conninfo_from_env()),
            "core-api": http_check(os.environ["SCC_CORE_URL"]),
        }
    return create_app("web", "SuppClaimCheck web", checks)
