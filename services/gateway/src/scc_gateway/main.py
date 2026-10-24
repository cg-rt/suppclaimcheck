"""The gateway: the only module that calls outside services (LLM, literature, alerts). In M1 it only has the ops endpoints (P1.01)."""

from fastapi import FastAPI

from scc_common.app import Check, create_app
from scc_common.db import conninfo_from_env, db_check


def build_app(checks: dict[str, Check] | None = None) -> FastAPI:
    if checks is None:
        checks = {"database": db_check(conninfo_from_env())}
    return create_app("gateway", "SuppClaimCheck gateway", checks)
