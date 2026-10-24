"""The committed OpenAPI specs match the code (D-035). Regenerate with: uv run python tools/openapi.py"""

import pytest

from openapi import ROOT, render, specs  # tools/ is on the test path

SPECS = specs()


@pytest.mark.parametrize("module", sorted(SPECS))
def test_committed_spec_is_up_to_date(module):
    committed = (ROOT / "services" / module / "openapi.json").read_text(
        encoding="utf-8"
    )
    assert committed == render(SPECS[module]), (
        f"services/{module}/openapi.json is stale: run uv run python tools/openapi.py"
    )
