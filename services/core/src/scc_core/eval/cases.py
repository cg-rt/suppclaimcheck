"""The eval set (P2.01): labelled cases in `evals/`, one TOML file each, plus `evals/evalset.toml`.

`evalset.toml` names the set version and the pass thresholds. Any change to a case bumps the version,
and results are only comparable within one version (docs/EVALS.md).
"""

import datetime
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Behaviour = Literal[
    "answer",  # a verdict and score
    "answer_with_safety_notice",  # a verdict and score, with a visible safety notice (P3.03)
    "clarify",  # ask which effect the user means, before searching (P3.01)
    "pick_claim",  # several claims: the user picks one on the confirm page
    "refuse",  # the policy message, before searching (P3.01)
    "suggest_spelling",  # "Did you mean ...?" on the confirm page (P2.12)
]
ANSWERS = {"answer", "answer_with_safety_notice"}
VERDICTS = Literal[
    "Supported", "Contradicted", "Mixed", "Insufficient evidence", "Not found"
]
SPLITS = {"tuning": "cases", "held_out": "held_out"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Expected(_Strict):
    behaviour: Behaviour
    verdicts: list[VERDICTS] = Field(default_factory=list)
    score_range: tuple[int, int] | None = None
    # Names the extraction may give the supplement; matched case-insensitively.
    supplement: list[str] = Field(default_factory=list)
    min_claims: int | None = None
    spelling_suggestion: str | None = None
    # Canaries: text that must not appear anywhere in the job's output.
    must_not_contain: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self):
        if self.behaviour in ANSWERS and not self.verdicts:
            raise ValueError("an answer case needs at least one expected verdict")
        if self.behaviour == "pick_claim" and not self.min_claims:
            raise ValueError("a pick_claim case needs min_claims")
        if self.behaviour == "suggest_spelling" and not self.spelling_suggestion:
            raise ValueError("a suggest_spelling case needs spelling_suggestion")
        if (
            self.score_range
            and not 0 <= self.score_range[0] <= self.score_range[1] <= 100
        ):
            raise ValueError("score_range must be two numbers from 0 to 100, low first")
        return self


class Label(_Strict):
    rationale: str
    pmids: list[str]
    # draft: written by the agent; approved: the owner agrees; disputed: see notes (C2.1).
    status: Literal["draft", "approved", "disputed"]
    drafted_on: datetime.date
    approved_on: datetime.date | None = None
    notes: str = ""


class EvalCase(_Strict):
    id: str
    category: Literal[
        "well_supported",
        "contradicted",
        "no_evidence",
        "vague",
        "multi_claim",
        "hidden_instructions",
        "unsafe",
        "safety_sensitive",
    ]
    input: str = Field(min_length=1)
    expected: Expected
    label: Label


class EvalSet(BaseModel):
    version: str
    thresholds: dict[str, float]
    cases: list[EvalCase]
    split: str


def load(root: Path, split: str = "tuning") -> EvalSet:
    """Loads one split. File names must match case IDs, and IDs must be unique across both splits."""
    meta = tomllib.loads((root / "evalset.toml").read_text("utf-8"))
    cases = []
    for path in sorted((root / SPLITS[split]).glob("*.toml")):
        case = EvalCase.model_validate(tomllib.loads(path.read_text("utf-8")))
        if case.id != path.stem:
            raise ValueError(f"{path.name} holds case {case.id}")
        cases.append(case)
    other = {
        p.stem
        for s, f in SPLITS.items()
        if s != split
        for p in (root / f).glob("*.toml")
    }
    if clash := other & {c.id for c in cases}:
        raise ValueError(f"case IDs in both splits: {sorted(clash)}")
    return EvalSet(
        version=meta["version"], thresholds=meta["thresholds"], cases=cases, split=split
    )
