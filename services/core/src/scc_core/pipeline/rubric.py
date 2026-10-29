"""Scoring rubrics: code computes the verdict and the 0-100 score, never the LLM (AGENTS.md section 6).

Inputs are only verified passages. D-002: 0 = consistent strong evidence against, 50 = balanced or too
weak to call, 100 = consistent strong evidence for.
- v0 (P1.08): counts verified passages; strength grows with their number.
- v1 (P2.04): weighs articles by evidence tier; see docs/METHOD.md. The config's `version` picks one.
"""

from dataclasses import asdict, dataclass

SUPPORTED, CONTRADICTED, MIXED = "Supported", "Contradicted", "Mixed"
INSUFFICIENT, NOT_FOUND = "Insufficient evidence", "Not found"


@dataclass(frozen=True)
class Score:
    verdict: str
    score: int
    articles_assessed: int
    supporting: int
    contradicting: int
    direction: float
    strength: float
    rubric_version: str

    def as_dict(self) -> dict:
        return asdict(self)


def score(
    articles_assessed: int, supporting: int, contradicting: int, config: dict
) -> Score:
    version = config["version"]
    if articles_assessed == 0:
        return Score(NOT_FOUND, 50, 0, 0, 0, 0.0, 0.0, version)
    total = supporting + contradicting
    if total == 0:
        return Score(INSUFFICIENT, 50, articles_assessed, 0, 0, 0.0, 0.0, version)
    direction = (supporting - contradicting) / total
    strength = min(1.0, total / config["full_strength_passages"])
    value = round(50 + 50 * direction * strength)
    if supporting and contradicting:
        verdict = MIXED
    elif supporting:
        verdict = SUPPORTED
    else:
        verdict = CONTRADICTED
    return Score(
        verdict,
        value,
        articles_assessed,
        supporting,
        contradicting,
        round(direction, 3),
        round(strength, 3),
        version,
    )


# Rubric v1 (P2.04): articles, not passages, are the unit of evidence. See docs/METHOD.md.


@dataclass(frozen=True)
class ArticleEvidence:
    """One assessed article: its tier and weight (P2.05) and its verified quotes for and against."""

    pmcid: str
    tier: str
    weight: float
    supporting: int
    contradicting: int

    @property
    def has_finding(self) -> bool:
        return self.supporting + self.contradicting > 0

    @property
    def stance(self) -> float:
        """+1 when every verified quote supports the claim, -1 when every one contradicts it."""
        total = self.supporting + self.contradicting
        return (self.supporting - self.contradicting) / total if total else 0.0


@dataclass(frozen=True)
class ScoreV1:
    verdict: str
    score: int
    articles_assessed: int
    supporting: int  # verified quotes, kept for the result page's counts
    contradicting: int
    direction: float
    strength: float
    rubric_version: str
    rule: str  # which verdict rule decided, in plain words
    weight_for: float
    weight_against: float
    human_weight: float
    full_strength_weight: float
    breakdown: list[dict]

    def as_dict(self) -> dict:
        return asdict(self)


def score_v1(articles: list[ArticleEvidence], config: dict) -> ScoreV1:
    """Verdict rules, checked in order, then the score: 50 + 50 x direction x strength.

    1. No article could be read: Not found.
    2. No article has a verified finding: Insufficient evidence.
    3. No finding comes from a human study: Insufficient evidence.
    4. Strength below `min_strength` (too little, or too weak, evidence): Insufficient evidence.
    5. The smaller side holds at least `mixed_min_share` of the weighted evidence: Mixed.
    6. Otherwise the direction's sign: Supported or Contradicted.
    """
    version = config["version"]
    found = [a for a in articles if a.has_finding]
    weight_for = sum(a.weight * max(a.stance, 0.0) for a in found)
    weight_against = sum(a.weight * max(-a.stance, 0.0) for a in found)
    total_weight = sum(a.weight for a in found)
    human_weight = sum(a.weight for a in found if a.tier in config["human_tiers"])
    direction = (weight_for - weight_against) / total_weight if total_weight else 0.0
    strength = min(1.0, total_weight / config["full_strength_weight"])
    value = round(50 + 50 * direction * strength)

    if not articles:
        verdict, rule = NOT_FOUND, "No open-access article could be read."
    elif not found:
        verdict, rule = (
            INSUFFICIENT,
            "No verified passage supports or contradicts the claim.",
        )
    elif human_weight == 0:
        verdict, rule = (
            INSUFFICIENT,
            "The only findings come from animal, lab or unclassified studies.",
        )
    elif strength < config["min_strength"]:
        verdict, rule = (
            INSUFFICIENT,
            "The evidence found is too little or too weak to call.",
        )
    elif (
        weight_for + weight_against
        == 0  # every article with findings is exactly balanced
        or min(weight_for, weight_against) / (weight_for + weight_against)
        >= config["mixed_min_share"]
    ):
        verdict, rule = MIXED, "Substantial evidence points each way."
    elif direction > 0:
        verdict, rule = SUPPORTED, "The weighted evidence supports the claim."
    else:
        verdict, rule = CONTRADICTED, "The weighted evidence contradicts the claim."

    return ScoreV1(
        verdict=verdict,
        score=value if found else 50,
        articles_assessed=len(articles),
        supporting=sum(a.supporting for a in articles),
        contradicting=sum(a.contradicting for a in articles),
        direction=round(direction, 3),
        strength=round(strength, 3),
        rubric_version=version,
        rule=rule,
        weight_for=round(weight_for, 3),
        weight_against=round(weight_against, 3),
        human_weight=round(human_weight, 3),
        full_strength_weight=config["full_strength_weight"],
        breakdown=[
            {**asdict(a), "stance": round(a.stance, 3), "counted": a.has_finding}
            for a in articles
        ],
    )
