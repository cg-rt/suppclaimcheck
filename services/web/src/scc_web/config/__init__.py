"""Versioned web config (P1.04). One file per version; `load()` returns the active one."""

import os
import tomllib
from functools import cache
from pathlib import Path

FOLDER = Path(__file__).parent
ACTIVE = "web-v0"


@cache
def load(version: str | None = None) -> dict:
    name = version or os.environ.get("SCC_WEB_CONFIG", ACTIVE)
    with (FOLDER / f"{name}.toml").open("rb") as f:
        config = tomllib.load(f)
    if config["version"] != name:
        raise ValueError(f"{name}.toml says it is {config['version']}")
    return config
