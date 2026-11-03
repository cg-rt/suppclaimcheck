"""Versioned pipeline config (P1.08). One file per version. `load()` reads a file (tests, tools, evals); the running module uses `load_active()`, the version active in the database (P2.09, D-048)."""

import os
import tomllib
from functools import cache
from pathlib import Path

import psycopg

from scc_common import config_store
from scc_common.config_store import Store

FOLDER = Path(__file__).parent
ACTIVE = "pipeline-v5"
# The database copy of these files, the active pointer and the audit (P2.09, D-048).
STORE = Store(
    schema="core",
    folder=FOLDER,
    prefix="pipeline-",
    shipped=ACTIVE,
    evaluated=True,
)


@cache
def load(version: str | None = None) -> dict:
    name = version or os.environ.get("SCC_PIPELINE_CONFIG", ACTIVE)
    with (FOLDER / f"{name}.toml").open("rb") as f:
        config = tomllib.load(f)
    if config["version"] != name:
        raise ValueError(f"{name}.toml says it is {config['version']}")
    return config


def load_active(conninfo: str) -> dict:
    """The active version from the database (D-048): what the running module uses. Files are for tests and tools."""
    with psycopg.connect(conninfo) as conn:
        return config_store.load_active(conn, STORE)
