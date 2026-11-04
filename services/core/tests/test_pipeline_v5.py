"""pipeline-v5 (P3.01-P3.03, D-050): v4 plus triage, and nothing else."""

from scc_core import config


def test_v5_was_active_until_v6():
    # pipeline-v6 (D-052) changes only the query; v5 stays loadable for old jobs and the evals.
    assert config.ACTIVE == "pipeline-v6"
    assert config.load("pipeline-v5")["version"] == "pipeline-v5"


def test_v5_changes_only_the_prompts_and_adds_guardrails():
    v4, v5 = config.load("pipeline-v4"), config.load("pipeline-v5")
    changed = {"version", "prompts", "guardrails"}
    assert {k: v for k, v in v5.items() if k not in changed} == {
        k: v for k, v in v4.items() if k not in changed
    }
    assert "guardrails" not in v4
    assert set(v5["guardrails"]) == {
        "injection_window_hours",
        "injection_max_checks",
        "sensitive_keywords",
    }


def test_v5_keeps_v4s_labelling_definitions_word_for_word():
    v4, v5 = config.load("pipeline-v4"), config.load("pipeline-v5")
    assert v5["prompts"]["assess"].startswith(v4["prompts"]["assess"].rstrip("\n"))
