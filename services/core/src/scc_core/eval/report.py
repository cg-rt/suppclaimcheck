"""Eval reports (P2.02): the run file, a Markdown summary, the baseline and the row in docs/EVALS.md."""

import json
from dataclasses import asdict
from pathlib import Path

from scc_core.eval.metrics import AT_MOST, CaseResult

LABELS = {
    "verdict_accuracy": "Verdict accuracy",
    "score_in_range": "Score in range",
    "extraction_accuracy": "Extraction (right supplement)",
    "quote_pass_rate": "Quote-verification pass rate",
    "citation_correctness": "Citation correctness",
    "behaviour_correctness": "Correct refusals and clarifications",
    "false_refusal_rate": "False refusals",
    "injection_resistance": "Injection resistance",
    "latency_p50_s": "Latency p50 (s)",
    "latency_p95_s": "Latency p95 (s)",
    "cost_per_check_eur": "Cost per check (EUR)",
}


def _fmt(name: str, value: float | None) -> str:
    if value is None:
        return "–"
    if name.endswith("_s"):
        return f"{value:.1f}"
    if name.endswith("_eur"):
        return f"{value:.4f}"
    return f"{value:.0%}"


def run_document(
    meta: dict,
    summary: dict,
    comparison: dict | None,
    results: list[CaseResult],
    repeats: list[list[CaseResult]] | None = None,
) -> dict:
    """The run file. With repeats, `cases` holds the merged results and `runs` every run's own."""
    document = {
        "run": meta,
        "summary": summary,
        "comparison": comparison,
        "cases": [asdict(r) for r in results],
    }
    if repeats and len(repeats) > 1:
        document["runs"] = [[asdict(r) for r in run] for run in repeats]
    return document


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=1, ensure_ascii=False) + "\n", "utf-8", newline="\n"
    )


def baseline_document(meta: dict, results: list[CaseResult]) -> dict:
    """The per-case outcome a later run is compared with; its diff in a PR shows which cases flipped."""
    return {
        "evalset": meta["evalset"],
        "split": meta["split"],
        "config_version": meta["config_version"],
        "run_id": meta["run_id"],
        "mode": meta["mode"],
        "cases": [
            {
                "id": r.id,
                "passed": r.passed,
                "verdict": r.observed.get("verdict"),
                "score": r.observed.get("score"),
                "behaviour": r.observed.get("behaviour"),
            }
            for r in results
        ],
    }


def markdown(
    meta: dict, summary: dict, comparison: dict | None, results: list[CaseResult]
) -> str:
    lines = [
        f"### Eval run `{meta['run_id']}`",
        "",
        " · ".join(
            [
                meta["mode"],
                meta["split"],
                meta["evalset"],
                meta["config_version"],
                meta["model"],
            ]
        )
        + f" · commit `{meta['commit']}`",
        "",
        f"**{summary['passed']} of {summary['cases']} cases pass**"
        + (f" · **{summary['stale']} stale**" if summary["stale"] else "")
        + f" · labels approved: {summary['labels_approved']} of {summary['cases']}"
        + f" · LLM cost EUR {summary['run_cost_eur']:.4f}"
        + (" (recorded)" if meta["mode"] == "replay" else ""),
        "",
    ]
    if summary.get("repeats", 1) > 1:
        lines += [
            f"**{summary['repeats']} runs, metrics are means.** Cases passing per run: "
            + ", ".join(str(p) for p in summary["passed_per_run"])
            + f" · verdict stability (same outcome in every run): {summary['stability']:.0%}",
            "",
        ]
    if meta.get("overrides"):
        lines += ["Overrides: " + ", ".join(f"`{o}`" for o in meta["overrides"]), ""]
    lines += [
        "| Metric | Value | Threshold |",
        "|---|---|---|",
    ]
    for name, label in LABELS.items():
        limit = meta["thresholds"].get(name)
        met = summary["thresholds_met"].get(name)
        mark = "" if met is None else " ✅" if met else " ❌"
        bound = (
            "–"
            if limit is None
            else ("≤ " if name in AT_MOST else "≥ ") + _fmt(name, limit)
        )
        lines.append(
            f"| {label} | {_fmt(name, summary['metrics'][name])} | {bound}{mark} |"
        )
    lines += ["", "| Category | Passed |", "|---|---|"]
    for category, counts in sorted(summary["by_category"].items()):
        lines.append(f"| {category} | {counts['passed']} of {counts['cases']} |")
    if comparison:
        failing_now = ", ".join(comparison["newly_failing"]) or "none"
        passing_now = ", ".join(comparison["newly_passing"]) or "none"
        lines += [
            "",
            f"**Against the baseline:** newly failing: {failing_now}; newly passing: {passing_now}.",
        ]
    failing = [r for r in results if not r.passed]
    if failing:
        lines += ["", "<details><summary>Failing cases</summary>", ""]
        lines += [f"- `{r.id}`: {'; '.join(r.reasons)}" for r in failing]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def evals_md_row(meta: dict, summary: dict) -> str:
    m = summary["metrics"]
    latency = f"{_fmt('latency_p50_s', m['latency_p50_s'])} / {_fmt('latency_p95_s', m['latency_p95_s'])}"
    cells = [
        meta["date"],
        meta["evalset"],
        meta["config_version"],
        meta["model"],
        meta["split"],
        str(summary["cases"]),
        _fmt("x", m["verdict_accuracy"]),
        _fmt("x", m["score_in_range"]),
        _fmt("x", m["quote_pass_rate"]),
        _fmt("x", m["citation_correctness"]),
        _fmt("x", m["behaviour_correctness"]),
        _fmt("x", m["injection_resistance"]),
        latency,
        _fmt("cost_per_check_eur", m["cost_per_check_eur"]),
        f"{summary['run_cost_eur']:.4f}",
        f"`{meta['commit']}`",
        meta.get("notes", ""),
    ]
    return "| " + " | ".join(cells) + " |"


def append_evals_md(path: Path, row: str) -> None:
    """Adds the row at the end of the table under '## Results'."""
    lines = path.read_text("utf-8").splitlines()
    start = lines.index("## Results")
    end = start + 1
    while end < len(lines) and not lines[end].startswith("|"):
        end += 1
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    lines.insert(end, row)
    path.write_text("\n".join(lines) + "\n", "utf-8", newline="\n")
