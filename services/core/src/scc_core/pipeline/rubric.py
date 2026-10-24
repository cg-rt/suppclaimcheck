"""Scoring rubric v0 (P1.08): code computes the verdict and the 0-100 score, never the LLM (AGENTS.md section 6).

Inputs are only the verified passages. D-002: 0 = consistent strong evidence against, 50 = balanced
or too weak to call, 100 = consistent strong evidence for. Weights by study design come with rubric v1 (P2.04).
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
