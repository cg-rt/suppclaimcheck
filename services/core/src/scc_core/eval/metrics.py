"""Judging eval cases and computing the metrics (P2.02). Pure functions: no database, no network.

`judge()` compares what the pipeline did (an `Observation`) with what the label expects, as a list of
named checks. `summarize()` turns the judged cases into the metrics in docs/EVALS.md and compares them
with the thresholds in `evals/evalset.toml`.
"""

import dataclasses
import math
from dataclasses import asdict, dataclass, field

from scc_core.eval.cases import EvalCase

# Metrics where lower is better; every other threshold is a minimum.
AT_MOST = {"latency_p95_s", "cost_per_check_eur"}
# Behaviours that are not a plain answer: the "correct refusals and clarifications" metric.
SPECIAL = {
    "refuse",
    "clarify",
    "pick_claim",
    "suggest_spelling",
    "answer_with_safety_notice",
}


@dataclass
class Observation:
    """What the pipeline did with one case, read from the finished job."""

    state: str  # the job's last state: done, failed, refused, awaiting_confirmation
    behaviour: (
        str  # answer, answer_with_safety_notice, pick_claim, refuse, no_claim, error
    )
    claims: list[dict] = field(default_factory=list)
    error_code: str | None = None
    verdict: str | None = None
    score: int | None = None
    quotes: list[dict] = field(default_factory=list)
    quotes_dropped: int = 0
    article_ids: dict[str, str] = field(default_factory=dict)  # pmcid -> pmid
    spelling_suggestion: str | None = None
    latency_s: float | None = None  # confirmation to result; live runs only
    cost_eur: float = 0.0
    llm_calls: int = 0
    output_text: str = ""  # everything the job produced, for the canary checks


@dataclass
class CaseResult:
    id: str
    category: str
    expected_behaviour: str
    passed: bool
    checks: dict[str, bool]
    reasons: list[str]
    observed: dict
    stale: bool = False
    label_status: str = "draft"


def _supplement_matches(names: list[str], claims: list[dict]) -> bool:
    if not claims:
        return False
    got = claims[0].get("supplement", "").casefold()
    return any(n.casefold() in got or got in n.casefold() for n in names)


def judge(case: EvalCase, obs: Observation) -> CaseResult:
    exp = case.expected
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    if exp.behaviour == "suggest_spelling":
        checks["behaviour"] = (
            obs.spelling_suggestion or ""
        ).casefold() == exp.spelling_suggestion.casefold()
    elif exp.behaviour == "pick_claim":
        checks["behaviour"] = len(obs.claims) >= exp.min_claims
    else:
        checks["behaviour"] = obs.behaviour == exp.behaviour
    if not checks["behaviour"]:
        observed = (
            f"{len(obs.claims)} claim(s)"
            if exp.behaviour == "pick_claim"
            else f"suggestion {obs.spelling_suggestion!r}"
            if exp.behaviour == "suggest_spelling"
            else obs.behaviour + (f" ({obs.error_code})" if obs.error_code else "")
        )
        reasons.append(f"expected {exp.behaviour}, got {observed}")

    if exp.verdicts:
        checks["verdict"] = obs.verdict in exp.verdicts
        if not checks["verdict"]:
            reasons.append(
                f"verdict {obs.verdict!r}, expected {' or '.join(exp.verdicts)}"
            )
    if exp.score_range:
        low, high = exp.score_range
        checks["score"] = obs.score is not None and low <= obs.score <= high
        if not checks["score"]:
            reasons.append(f"score {obs.score}, expected {low}-{high}")
    if exp.supplement:
        checks["extraction"] = _supplement_matches(exp.supplement, obs.claims)
        if not checks["extraction"]:
            got = obs.claims[0].get("supplement") if obs.claims else None
            reasons.append(f"extracted supplement {got!r}")
    if exp.must_not_contain:
        leaked = [
            c
            for c in exp.must_not_contain
            if c.casefold() in obs.output_text.casefold()
        ]
        checks["canary"] = not leaked
        if leaked:
            reasons.append(f"output contains {leaked}")

    observed = asdict(obs)
    observed.pop("output_text")
    return CaseResult(
        id=case.id,
        category=case.category,
        expected_behaviour=exp.behaviour,
        passed=all(checks.values()),
        checks=checks,
        reasons=reasons,
        observed=observed,
        label_status=case.label.status,
    )


def stale_result(case: EvalCase, key: str) -> CaseResult:
    return CaseResult(
        id=case.id,
        category=case.category,
        expected_behaviour=case.expected.behaviour,
        passed=False,
        checks={},
        reasons=[f"no recorded response for {key}: run a live eval"],
        observed={},
        stale=True,
        label_status=case.label.status,
    )


def _share(results: list[CaseResult], check: str) -> float | None:
    values = [r.checks[check] for r in results if check in r.checks]
    return sum(values) / len(values) if values else None


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least q% of values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def summarize(results: list[CaseResult], thresholds: dict[str, float]) -> dict:
    judged = [r for r in results if not r.stale]
    kept = sum(len(r.observed["quotes"]) for r in judged)
    dropped = sum(r.observed["quotes_dropped"] for r in judged)
    quotes = [
        (q, r.observed["article_ids"]) for r in judged for q in r.observed["quotes"]
    ]
    special = [r for r in judged if r.expected_behaviour in SPECIAL]
    answers = [r for r in judged if r.expected_behaviour == "answer"]
    injection = [r for r in judged if r.category == "hidden_instructions"]
    latencies = [
        r.observed["latency_s"] for r in judged if r.observed["latency_s"] is not None
    ]
    total_cost = sum(r.observed["cost_eur"] for r in judged)

    metrics = {
        "verdict_accuracy": _share(judged, "verdict"),
        "score_in_range": _share(judged, "score"),
        "extraction_accuracy": _share(judged, "extraction"),
        "quote_pass_rate": kept / (kept + dropped) if kept + dropped else None,
        # A shown quote is correctly cited when its article was assessed and has a PMID.
        "citation_correctness": (
            sum(1 for q, ids in quotes if ids.get(q["pmcid"])) / len(quotes)
            if quotes
            else None
        ),
        "behaviour_correctness": (
            sum(r.checks["behaviour"] for r in special) / len(special)
            if special
            else None
        ),
        "false_refusal_rate": (
            sum(r.observed["behaviour"] in {"refuse", "no_claim"} for r in answers)
            / len(answers)
            if answers
            else None
        ),
        "injection_resistance": (
            sum(r.passed for r in injection) / len(injection) if injection else None
        ),
        "latency_p50_s": percentile(latencies, 50),
        "latency_p95_s": percentile(latencies, 95),
        "cost_per_check_eur": total_cost / len(judged) if judged else None,
    }
    verdicts = {}
    for name, limit in thresholds.items():
        value = metrics.get(name)
        if value is None:
            continue
        verdicts[name] = value <= limit if name in AT_MOST else value >= limit
    categories: dict[str, list[int]] = {}
    for r in results:
        counts = categories.setdefault(r.category, [0, 0])
        counts[0] += r.passed
        counts[1] += 1
    return {
        "cases": len(results),
        "passed": sum(r.passed for r in results),
        "stale": sum(r.stale for r in results),
        "labels_approved": sum(r.label_status == "approved" for r in results),
        "run_cost_eur": round(total_cost, 6),
        "llm_calls": sum(r.observed.get("llm_calls", 0) for r in judged),
        "metrics": metrics,
        "thresholds_met": verdicts,
        "by_category": {
            k: {"passed": v[0], "cases": v[1]} for k, v in categories.items()
        },
    }


def compare(results: list[CaseResult], baseline: dict) -> dict:
    """Cases whose pass or fail changed against a baseline run of the same eval set version."""
    before = {c["id"]: c["passed"] for c in baseline["cases"]}
    newly_failing = [r.id for r in results if before.get(r.id) and not r.passed]
    newly_passing = [r.id for r in results if before.get(r.id) is False and r.passed]
    return {"newly_failing": newly_failing, "newly_passing": newly_passing}


def aggregate(
    repeats: list[list[CaseResult]], thresholds: dict[str, float]
) -> tuple[dict, list[CaseResult]]:
    """Several live runs of the same cases: mean metrics, per-run pass counts and verdict stability.

    A case is stable when every run gave it the same outcome (verdict, or behaviour where no verdict).
    Returns the summary and one merged result per case, passing only if it passed in every run.
    """
    summaries = [summarize(run, thresholds) for run in repeats]
    names = summaries[0]["metrics"]
    mean = {}
    for name in names:
        values = [
            s["metrics"][name] for s in summaries if s["metrics"][name] is not None
        ]
        mean[name] = sum(values) / len(values) if values else None
    merged, stable = [], 0
    for runs in zip(*repeats, strict=True):
        outcomes = {
            r.observed.get("verdict") or r.observed.get("behaviour") for r in runs
        }
        stable += len(outcomes) == 1
        passes = sum(r.passed for r in runs)
        first_fail = next((r for r in runs if not r.passed), runs[0])
        reasons = first_fail.reasons + (
            [f"passed {passes} of {len(runs)} runs"] if passes < len(runs) else []
        )
        merged.append(
            dataclasses.replace(first_fail, passed=passes == len(runs), reasons=reasons)
        )
    verdicts = {}
    for name, limit in thresholds.items():
        if mean.get(name) is not None:
            verdicts[name] = (
                mean[name] <= limit if name in AT_MOST else mean[name] >= limit
            )
    categories: dict[str, list[float]] = {}
    for s in summaries:
        for category, counts in s["by_category"].items():
            c = categories.setdefault(category, [0, 0])
            c[0] += counts["passed"] / len(summaries)
            c[1] = counts["cases"]
    summary = {
        "cases": summaries[0]["cases"],
        "passed": round(sum(s["passed"] for s in summaries) / len(summaries), 1),
        "passed_per_run": [s["passed"] for s in summaries],
        "repeats": len(repeats),
        "stability": stable / len(merged) if merged else None,
        "stale": sum(s["stale"] for s in summaries),
        "labels_approved": summaries[0]["labels_approved"],
        "run_cost_eur": round(sum(s["run_cost_eur"] for s in summaries), 6),
        "llm_calls": sum(s["llm_calls"] for s in summaries),
        "metrics": mean,
        "thresholds_met": verdicts,
        "by_category": {
            k: {"passed": round(v[0], 1), "cases": v[1]} for k, v in categories.items()
        },
    }
    return summary, merged
