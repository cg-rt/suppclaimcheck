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


def test_ci_and_dev_use_the_images_python():
    """uv picks the interpreter from .python-version; without it CI drifted to 3.15 while the images run 3.14."""
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    for dockerfile in sorted(ROOT.glob("services/*/Dockerfile")):
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if line.startswith("FROM python:"):
                image_python = line.removeprefix("FROM python:").split("-")[0]
                assert image_python.rsplit(".", 1)[0] == pinned, dockerfile
