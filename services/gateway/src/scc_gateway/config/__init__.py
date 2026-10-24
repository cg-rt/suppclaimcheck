"""Versioned gateway config (P1.06). One file per version; `load()` returns the active one."""

import os
import tomllib
from functools import cache
from pathlib import Path

FOLDER = Path(__file__).parent
ACTIVE = "gateway-v0"


@cache
def load(version: str | None = None) -> dict:
    name = version or os.environ.get("SCC_GATEWAY_CONFIG", ACTIVE)
    with (FOLDER / f"{name}.toml").open("rb") as f:
        config = tomllib.load(f)
    if config["version"] != name:
        raise ValueError(f"{name}.toml says it is {config['version']}")
    # For drills and the dev cap test: SCC_BUDGET_CAP_OVERRIDE_EUR lowers every lane's cap.
    # It can never raise one: raising a cap is the owner's decision (AGENTS.md section 6).
    override = os.environ.get("SCC_BUDGET_CAP_OVERRIDE_EUR")
    if override:
        for key in [k for k in config["budget"] if k.endswith("_monthly_cap_eur")]:
            config["budget"][key] = min(config["budget"][key], float(override))
    return config
