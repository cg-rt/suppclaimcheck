"""P2.01-P2.03 parts that need no database: the eval set, judging, metrics, replay keys and reports."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from scc_core.eval import cases, report
from scc_core.eval.cases import EvalCase
from scc_core.eval.metrics import Observation, compare, judge, percentile, summarize
from scc_core.eval.replay import EvalTransport, StaleRecording, request_key

EVALS = Path(__file__).resolve().parents[3] / "evals"


def _case(**expected) -> EvalCase:
    return EvalCase.model_validate(
        {
            "id": "t-01",
            "category": "well_supported",
            "input": "Creatine makes older adults stronger.",
            "expected": expected,
            "label": {
                "rationale": "r",
                "pmids": [],
                "status": "draft",
                "drafted_on": "2026-10-28",
            },
        }
    )


def _answer(verdict="Supported", score=75, **kw) -> Observation:
    return Observation(
        state="done",
        behaviour="answer",
        claims=[{"supplement": "Creatine monohydrate", "effect": "strength"}],
        verdict=verdict,
        score=score,
        **kw,
    )


def test_eval_set_v1_has_22_tuning_and_8_held_out_cases_one_per_category():
    tuning = cases.load(EVALS, "tuning")
    held_out = cases.load(EVALS, "held_out")
    assert (tuning.version, len(tuning.cases), len(held_out.cases)) == (
        "evalset-v1",
        22,
        8,
    )
    # Stratified: every category has tuning cases and exactly one held-out case.
    assert sorted(c.category for c in held_out.cases) == sorted(
        {c.category for c in tuning.cases}
    )
    # Every answerable case cites literature, so the owner can check the label.
    for case in tuning.cases + held_out.cases:
        if case.expected.verdicts and case.category != "no_evidence":
            assert case.label.pmids, case.id


def test_case_rules_are_enforced():
    with pytest.raises(ValidationError, match="expected verdict"):
        _case(behaviour="answer")
    with pytest.raises(ValidationError, match="min_claims"):
        _case(behaviour="pick_claim")
    with pytest.raises(ValidationError, match="score_range"):
        _case(behaviour="answer", verdicts=["Supported"], score_range=[80, 60])
    with pytest.raises(ValidationError):
        _case(behaviour="answer", verdicts=["Proven"])


def test_judge_checks_verdict_score_extraction_and_canaries():
    case = _case(
        behaviour="answer",
        verdicts=["Supported"],
        score_range=[60, 100],
        supplement=["creatine"],
        must_not_contain=["PINEAPPLE-7731"],
    )
    assert judge(case, _answer()).passed

    wrong = judge(
        case, _answer(verdict="Mixed", score=55, output_text="... pineapple-7731 ...")
    )
    assert not wrong.passed
    assert wrong.checks == {
        "behaviour": True,
        "verdict": False,
        "score": False,
        "extraction": True,
        "canary": False,
    }
    assert wrong.reasons == [
        "verdict 'Mixed', expected Supported",
        "score 55, expected 60-100",
        "output contains ['PINEAPPLE-7731']",
    ]


def test_judge_behaviours():
    refuse = _case(behaviour="refuse")
    assert judge(refuse, Observation(state="refused", behaviour="refuse")).passed
    answered = judge(refuse, _answer())
    assert answered.reasons == ["expected refuse, got answer"]

    pick = _case(behaviour="pick_claim", min_claims=2)
    assert judge(
        pick,
        Observation(
            state="awaiting_confirmation", behaviour="pick_claim", claims=[{}, {}]
        ),
    ).passed
    assert not judge(
        pick,
        Observation(
            state="awaiting_confirmation", behaviour="single_claim", claims=[{}]
        ),
    ).passed

    spell = _case(behaviour="suggest_spelling", spelling_suggestion="ashwagandha")
    assert judge(spell, _answer(spelling_suggestion="Ashwagandha")).passed
    assert not judge(spell, _answer()).passed


def test_summary_metrics_and_thresholds():
    supported = _case(behaviour="answer", verdicts=["Supported"], score_range=[60, 100])
    refuse = _case(behaviour="refuse")
    quote = {"pmcid": "PMC1", "label": "supports"}
    results = [
        judge(supported, _answer(quotes=[quote, quote], quotes_dropped=1, article_ids={"PMC1": "11"},
                                 latency_s=10.0, cost_eur=0.002, llm_calls=2)),
        judge(supported, _answer(verdict="Mixed", score=50, latency_s=30.0, cost_eur=0.004, llm_calls=2)),
        judge(refuse, _answer(latency_s=20.0, cost_eur=0.003, llm_calls=2)),
    ]  # fmt: skip
    summary = summarize(
        results, {"verdict_accuracy": 0.75, "quote_pass_rate": 0.6, "latency_p95_s": 25}
    )
    m = summary["metrics"]
    assert (summary["cases"], summary["passed"]) == (3, 1)
    assert m["verdict_accuracy"] == 0.5
    assert m["quote_pass_rate"] == pytest.approx(2 / 3)
    assert m["citation_correctness"] == 1.0
    assert m["behaviour_correctness"] == 0.0  # the refuse case was answered
    assert m["false_refusal_rate"] == 0.0
    assert (m["latency_p50_s"], m["latency_p95_s"]) == (20.0, 30.0)
    assert m["cost_per_check_eur"] == pytest.approx(0.003)
    assert summary["thresholds_met"] == {
        "verdict_accuracy": False,
        "quote_pass_rate": True,
        "latency_p95_s": False,
    }


def test_percentile_is_nearest_rank():
    assert percentile([], 95) is None
    assert percentile([5.0], 95) == 5.0
    assert percentile([float(i) for i in range(1, 21)], 95) == 19.0
    assert percentile([float(i) for i in range(1, 21)], 50) == 10.0


def test_compare_lists_cases_that_flipped():
    case = _case(behaviour="answer", verdicts=["Supported"])
    results = [judge(case, _answer(verdict="Mixed"))]
    assert compare(results, {"cases": [{"id": "t-01", "passed": True}]}) == {
        "newly_failing": ["t-01"],
        "newly_passing": [],
    }


def test_request_key_ignores_what_changes_every_run():
    def request(**body):
        return httpx.Request("POST", "http://gateway/llm/structured", json=body)

    same = request(job_id="a", user_id=0, lane="eval", instructions="p1", data="d")
    assert request_key(same) == request_key(
        request(job_id="b", user_id=7, lane="live", instructions="p1", data="d")
    )
    assert request_key(same) != request_key(
        request(job_id="a", instructions="p2", data="d")
    )


def _run(coro):
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)


def test_record_then_replay_round_trip(tmp_path):
    def gateway(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/llm/structured":
            return httpx.Response(
                200, json={"output": {"claims": []}, "cost_eur": 0.0012}
            )
        return httpx.Response(200, json={"count": 0, "pmids": []})

    async def calls(transport):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway"
        ) as client:
            llm = await client.post(
                "/llm/structured", json={"job_id": "j1", "instructions": "p1"}
            )
            search = await client.post("/literature/search", json={"query": "q"})
            alert = await client.post("/notify", json={"event": "job_failed"})
        return llm.json(), search.json(), alert.json()

    live = EvalTransport("live", tmp_path, inner=httpx.MockTransport(gateway))
    recorded = _run(calls(live))
    live.save()
    assert live.cost_eur == {"j1": 0.0012} and live.llm_calls == {"j1": 1}
    # Alerts never leave the runner.
    assert recorded[2] == {"sent": False, "reason": "eval"}
    assert len(list(tmp_path.glob("*.json.gz"))) == 2

    replay = EvalTransport("replay", tmp_path)
    assert _run(calls(replay)) == recorded

    async def changed_prompt():
        async with httpx.AsyncClient(
            transport=replay, base_url="http://gateway"
        ) as client:
            await client.post(
                "/llm/structured", json={"job_id": "j1", "instructions": "p2"}
            )

    with pytest.raises(StaleRecording):
        _run(changed_prompt())


def test_evals_md_row_lands_at_the_end_of_the_results_table(tmp_path):
    doc = tmp_path / "EVALS.md"
    doc.write_text(
        "# EVALS\n\n## Results\n| Date | n |\n|---|---|\n| old | 1 |\n\n## Comparisons\n",
        "utf-8",
    )
    report.append_evals_md(doc, "| new | 2 |")
    assert doc.read_text("utf-8") == (
        "# EVALS\n\n## Results\n| Date | n |\n|---|---|\n| old | 1 |\n| new | 2 |\n\n## Comparisons\n"
    )


def test_markdown_summary_marks_thresholds():
    case = _case(behaviour="answer", verdicts=["Supported"])
    results = [judge(case, _answer())]
    summary = summarize(results, {"verdict_accuracy": 0.75})
    meta = {
        "run_id": "r1", "mode": "replay", "split": "tuning", "evalset": "evalset-v1",
        "config_version": "pipeline-v0", "model": "m", "commit": "abc", "thresholds": {"verdict_accuracy": 0.75},
    }  # fmt: skip
    text = report.markdown(meta, summary, None, results)
    assert "| Verdict accuracy | 100% | ≥ 75% ✅ |" in text
    assert "1 of 1 cases pass" in text
    json.dumps(report.run_document(meta, summary, None, results))  # serialisable
