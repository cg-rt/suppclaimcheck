"""docs/ERRORS.md lists every error code the modules can return or record (P3.05, D-053)."""

import re
from pathlib import Path

from scc_core.pipeline import run, triage
from scc_gateway import main as gateway_main
from scc_web import admin_views, views

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = (ROOT / "docs" / "ERRORS.md").read_text(encoding="utf-8")


def documented() -> set[str]:
    return set(re.findall(r"^\| `([a-z_]+)` \|", CATALOGUE, flags=re.MULTILINE))


def in_code() -> set[str]:
    codes = (
        set(run.MESSAGES)
        | set(triage.REFUSALS)
        | set(triage.CLARIFICATIONS)
        | set(gateway_main._STATUS)
        | {c for c in admin_views.CODES if ":" not in c}
        | set(views.SKIP_REASONS)
    )
    # Every error detail a module raises: {"code": "...", ...}.
    for path in [
        *ROOT.glob("services/*/src/**/*.py"),
        *ROOT.glob("libs/*/src/**/*.py"),
    ]:
        codes |= set(
            re.findall(r'"code": "([a-z_]+)"', path.read_text(encoding="utf-8"))
        )
    # "error" is only the clients' fallback when a reply has no code; it never reaches a user.
    return codes - {"error"}


def test_every_code_is_in_the_catalogue():
    missing = sorted(in_code() - documented())
    assert not missing, f"add these codes to docs/ERRORS.md: {missing}"


def test_the_catalogue_names_no_code_that_no_longer_exists():
    stale = sorted(documented() - in_code())
    assert not stale, f"these codes in docs/ERRORS.md are not in the code: {stale}"
