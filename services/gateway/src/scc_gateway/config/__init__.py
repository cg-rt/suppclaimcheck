"""Versioned gateway config (P1.06). One file per version. `load()` reads a file (tests, tools, evals); the running module uses `load_active()`, the version active in the database (P2.09, D-048)."""

import os
import tomllib
from functools import cache
from pathlib import Path

import psycopg

from scc_common import config_store
from scc_common.config_store import Store

FOLDER = Path(__file__).parent
ACTIVE = "gateway-v2"
# The database copy of these files, the active pointer and the audit (P2.09, D-048).
STORE = Store(
    schema="gateway",
    folder=FOLDER,
    prefix="gateway-",
    shipped=ACTIVE,
    evaluated=False,
    restart_to_apply="gateway",
)


@cache
def load(version: str | None = None) -> dict:
    name = version or os.environ.get("SCC_GATEWAY_CONFIG", ACTIVE)
    with (FOLDER / f"{name}.toml").open("rb") as f:
        config = tomllib.load(f)
    if config["version"] != name:
        raise ValueError(f"{name}.toml says it is {config['version']}")
    return _cap_override(config)


def load_active(conninfo: str) -> dict:
    """The active version from the database (D-048): what the running gateway uses. Files are for tests and tools."""
    with psycopg.connect(conninfo) as conn:
        return _cap_override(config_store.load_active(conn, STORE))


def _cap_override(config: dict) -> dict:
    # For drills and the dev cap test: SCC_BUDGET_CAP_OVERRIDE_EUR lowers every lane's cap.
    # It can never raise one: raising a cap is the owner's decision (AGENTS.md section 6).
    override = os.environ.get("SCC_BUDGET_CAP_OVERRIDE_EUR")
    if override:
        for key in [k for k in config["budget"] if k.endswith("_monthly_cap_eur")]:
            config["budget"][key] = min(config["budget"][key], float(override))
    return config
