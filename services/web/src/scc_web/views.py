"""Turns core's job data into what the pages show. Plain words, never more certain than the evidence."""

from datetime import datetime

# The user-facing name of each stage, in the order a check goes through them.
STAGES = {
    "submitted": "Received your text",
    "extracting": "Reading your text for claims",
    "awaiting_confirmation": "Waiting for you to confirm the claim",
    "searching": "Searching PubMed for open-access articles",
    "retrieving": "Fetching the article and checking its licence",
    "assessing": "Reading passages for evidence",
    "verifying": "Checking every quote word for word",
    "scoring": "Working out the verdict",
    "done": "Result ready",
    "failed": "Stopped",
    "refused": "Not checked",
}
BEFORE_CONFIRM = ["submitted", "extracting", "awaiting_confirmation"]
AFTER_CONFIRM = ["searching", "retrieving", "assessing", "verifying", "scoring", "done"]
# States where the progress stream stops and the page reloads to show what comes next.
SETTLED = {"awaiting_confirmation", "done", "failed", "refused"}

VERDICT_CLASS = {
    "Supported": "supported",
    "Contradicted": "contradicted",
    "Mixed": "mixed",
    "Insufficient evidence": "none",
    "Not found": "none",
}


def progress(state: str, events: list[dict]) -> list[dict]:
    """One row per stage of the current phase: done, current or waiting, with the time it started."""
    stages = BEFORE_CONFIRM if state in BEFORE_CONFIRM else AFTER_CONFIRM
    started = {}
    for event in events:
        started.setdefault(event["state"], event["at"])
    current_index = stages.index(state) if state in stages else len(stages)
    rows = []
    for i, name in enumerate(stages):
        status = (
            "done"
            if i < current_index
            else "current"
            if i == current_index
            else "waiting"
        )
        if state == "done" and name == "done":
            status = "done"
        at = started.get(name)
        rows.append(
            {
                "state": name,
                "label": STAGES[name],
                "status": status,
                "at": _clock(at) if at else None,
            }
        )
    return rows


def _clock(value: str | datetime) -> str:
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment.strftime("%H:%M:%S")


TIER_NAMES = {
    "systematic_review": "Systematic review or meta-analysis",
    "rct": "Randomised controlled trial",
    "human_trial": "Other human trial",
    "observational": "Observational study",
    "human_other": "Human study, design not stated",
    "narrative_review": "Narrative review",
    "unclassified": "Study type unknown",
    "animal": "Animal study",
    "in_vitro": "Lab study (cells)",
    "other": "Editorial, letter or case report",
}


def _v1(result: dict) -> bool:
    return result.get("rubric_version") == "rubric-v1"


def explain(result: dict) -> str:
    if _v1(result):
        n = result["articles_assessed"]
        if n == 0:
            return "No open-access article that could be read was found for this claim."
        found = sum(1 for a in result["breakdown"] if a["counted"])
        articles = f"{n} article" + ("" if n == 1 else "s")
        return f"{result['rule']} {found} of the {articles} read reported a finding about this claim."
    verdict, s, c, n = (
        result["verdict"],
        result["supporting"],
        result["contradicting"],
        result["articles_assessed"],
    )
    articles = f"{n} article" + ("" if n == 1 else "s")

    def passages(k: int) -> str:
        return f"{k} passage" + ("" if k == 1 else "s")

    if verdict == "Supported":
        return f"{passages(s).capitalize()} in the {articles} read support the claim, and none contradict it."
    if verdict == "Contradicted":
        return f"{passages(c).capitalize()} in the {articles} read contradict the claim, and none support it."
    if verdict == "Mixed":
        return f"The evidence points both ways: {passages(s)} support the claim and {passages(c)} contradict it."
    if verdict == "Insufficient evidence":
        return f"The {articles} read didn't say clearly either way, so there is not enough to call it."
    return "No open-access article that could be read was found for this claim."


def score_steps(result: dict, full_strength: int | None = None) -> list[str]:
    """The score worked out in the open, line by line, from the rubric's own numbers."""
    if _v1(result):
        return _score_steps_v1(result)
    s, c = result["supporting"], result["contradicting"]
    if s + c == 0:
        return [
            "No verified passage supports or contradicts the claim, so the score stays at 50."
        ]
    return [
        f"Direction: ({s} supporting − {c} contradicting) ÷ {s + c} = {result['direction']:+.2f}",
        f"Strength: {s + c} verified passages, {result['strength']:.2f} of full strength",
        f"Score: 50 + 50 × {result['direction']:+.2f} × {result['strength']:.2f} = {result['score']}",
    ]


def _score_steps_v1(result: dict) -> list[str]:
    counted = [a for a in result["breakdown"] if a["counted"]]
    if not counted:
        return [
            "No verified passage supports or contradicts the claim, so the score stays at 50."
        ]
    steps = [
        f"{TIER_NAMES.get(a['tier'], a['tier'])}, weight {a['weight']:.2f}: "
        f"{a['supporting']} for, {a['contradicting']} against, stance {a['stance']:+.2f}"
        for a in counted
    ]
    total = sum(a["weight"] for a in counted)
    w_for, w_against = result["weight_for"], result["weight_against"]
    d, s, full = result["direction"], result["strength"], result["full_strength_weight"]
    steps += [
        f"Direction: weighted for {w_for:.2f} − against {w_against:.2f}, ÷ total weight {total:.2f} = {d:+.2f}",
        f"Strength: total weight {total:.2f} ÷ {full:.2f} for full strength = {s:.2f} (at most 1)",
        f"Score: 50 + 50 × {d:+.2f} × {s:.2f} = {result['score']}",
        f"Verdict: {result['rule']}",
    ]
    return steps


def citation(article: dict) -> str:
    parts = [article.get("authors"), article.get("journal"), article.get("year")]
    return ". ".join(str(p).rstrip(".") for p in parts if p)


def licence_label(licence: str | None) -> str:
    return (licence or "unknown licence").upper().replace("CC0", "CC0 1.0")


def result_view(job: dict) -> dict:
    result = job["result"]
    articles = {a["pmcid"]: a for a in result["articles"]}
    quotes = []
    for quote in result["quotes"]:
        article = articles.get(quote["pmcid"], {})
        quotes.append(
            {
                **quote,
                "citation": citation(article),
                "title": article.get("title"),
                "licence": licence_label(article.get("license")),
            }
        )
    skipped = result["searched"].get("skipped", {})
    return {
        "verdict": result["verdict"],
        "verdict_class": VERDICT_CLASS.get(result["verdict"], "none"),
        "explanation": explain(result),
        "score": result["score"],
        "steps": score_steps(result),
        "result": result,
        "supporting": [q for q in quotes if q["label"] == "supports"],
        "contradicting": [q for q in quotes if q["label"] == "contradicts"],
        "articles": [
            {**a, "citation": citation(a), "licence": licence_label(a.get("license"))}
            for a in result["articles"]
        ],
        "skipped": skipped,
        "skipped_total": sum(skipped.values()),
        "not_read": result.get("found_not_assessed", []),
    }
