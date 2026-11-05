"""pipeline-v8 (P3.06, D-054): v7 plus the retention periods, and nothing else."""

from scc_core import config


def test_v8_adds_only_retention_to_v7():
    v7, v8 = config.load("pipeline-v7"), config.load("pipeline-v8")
    changed = {"version", "retention"}
    assert {k: v for k, v in v8.items() if k not in changed} == {
        k: v for k, v in v7.items() if k not in changed
    }
    assert v8["retention"] == {"check_days": 180, "queue_days": 30}
    assert "retention" not in v7
