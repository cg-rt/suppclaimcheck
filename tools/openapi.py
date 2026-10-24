"""Writes each HTTP module's OpenAPI spec to services/<module>/openapi.json (D-035).

The specs are generated from the code; tests/test_openapi.py fails when a committed spec is out of date.
Needs the modules installed: uv sync --all-packages

Run: uv run python tools/openapi.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def specs() -> dict[str, dict]:
    from scc_core.api import build_app as core_app
    from scc_gateway.main import build_app as gateway_app
    from scc_web.main import build_app as web_app

    # No dependency checks or resources are needed to describe the API.
    return {
        "web": web_app(checks={}).openapi(),
        "core": core_app(checks={}).openapi(),
        "gateway": gateway_app(checks={}).openapi(),
    }


def render(spec: dict) -> str:
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    for module, spec in specs().items():
        path = ROOT / "services" / module / "openapi.json"
        path.write_text(render(spec), encoding="utf-8", newline="\n")
        print(f"openapi: wrote {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
