"""One version for the whole app (D-034): VERSION and every package agree."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_package_has_the_app_version():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    root = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for member in root["tool"]["uv"]["workspace"]["members"]:
        project = tomllib.loads(
            (ROOT / member / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        assert project["version"] == version, member
