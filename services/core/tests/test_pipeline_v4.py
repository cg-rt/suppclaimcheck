"""P2.07: pipeline-v4 is the measured arm (v3 with keyword weight 0.7) and nothing else (D-047)."""

from scc_core import config
from scc_core.eval.__main__ import apply_overrides


def test_v4_is_active_and_hybrid():
    assert config.ACTIVE == "pipeline-v4"
    retrieval = config.load("pipeline-v4")["retrieval"]
    assert retrieval["mode"] == "hybrid"
    assert retrieval["keyword_weight"] == 0.7


def test_v4_equals_the_measured_arm():
    # The P2.07 variant arm ran pipeline-v3 with `--set retrieval.keyword_weight=0.7`.
    arm = apply_overrides(config.load("pipeline-v3"), ["retrieval.keyword_weight=0.7"])
    v4 = dict(config.load("pipeline-v4"))
    assert {**v4, "version": arm["version"]} == arm
