"""pipeline-v7 (P3.05, D-053): v6 plus result reuse and the stall limit, and nothing else."""

from scc_core import config


def test_v7_adds_only_cache_and_resilience_to_v6():
    v6, v7 = config.load("pipeline-v6"), config.load("pipeline-v7")
    changed = {"version", "cache", "resilience"}
    assert {k: v for k, v in v7.items() if k not in changed} == {
        k: v for k, v in v6.items() if k not in changed
    }
    assert v7["cache"] == {"result_days": 7}
    assert v7["resilience"] == {"stall_minutes": 20}
    assert "cache" not in v6 and "resilience" not in v6
