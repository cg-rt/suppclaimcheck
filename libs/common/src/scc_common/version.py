"""The app version and git SHA, reported by `/version` and in every log line (D-034: one version for the whole app)."""

import os
from functools import cache
from pathlib import Path


def _find_version_file() -> Path | None:
    configured = os.environ.get("SCC_VERSION_FILE")
    if configured:
        return Path(configured)
    # Outside a container: the VERSION file at the repo root.
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "VERSION"
        if candidate.is_file():
            return candidate
    return None


@cache
def app_version() -> str:
    path = _find_version_file()
    if path is None or not path.is_file():
        return "unknown"
    return path.read_text(encoding="utf-8").strip()


def version_info(service: str) -> dict[str, str]:
    return {
        "service": service,
        "version": app_version(),
        "git_sha": os.environ.get("SCC_GIT_SHA", "unknown"),
    }
