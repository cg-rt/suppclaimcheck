"""Triage (P3.01-P3.03, D-050): code turns the model's labels into refuse, clarify or continue.

The rules and the user-facing messages are docs/POLICY.md; change both together. The model only fills
in `TriagedExtraction.triage` and each claim's `specific`; nothing here calls it.
"""

import re
from dataclasses import dataclass
from typing import Literal

from scc_core.models import TriagedExtraction

REFUSALS = {
    "refuse_personal_advice": (
        "SuppClaimCheck can't give personal medical or dosing advice. It checks what research says "
        "about a claim. For advice about your health, please talk to a doctor or pharmacist."
    ),
    "refuse_marketing": "SuppClaimCheck checks claims; it doesn't write them.",
    "refuse_off_topic": "SuppClaimCheck only checks claims about dietary supplements.",
    "refuse_system_prompt": "SuppClaimCheck can't share how it is set up.",
    "refuse_abusive": "SuppClaimCheck can't help with this.",
}
CLARIFICATIONS = {
    "clarify_vague": (
        "Which effect do you want to check? Research tests specific effects, for example "
        "“turmeric reduces knee pain in osteoarthritis”. Start a new check that names one."
    ),
    "clarify_no_claim": (
        "No claim about a dietary supplement was found. Start a new check with a sentence that names "
        "the supplement and what it is said to do."
    ),
}
# In display order: a check can carry several.
SAFETY_NOTICES = {
    "pregnancy": (
        "Research on supplements in pregnancy and breastfeeding is limited, and some supplements can "
        "harm a baby. Talk to your midwife, doctor or pharmacist before taking any."
    ),
    "children": (
        "Children react to supplements differently from adults, and doses for adults can harm them. "
        "Ask a doctor or pharmacist before giving a child any supplement."
    ),
    "serious_illness": (
        "If you have a serious illness, a supplement can interfere with your treatment. Talk to your "
        "doctor before taking it, and don't stop any treatment because of a result here."
    ),
    "high_dose": (
        "High doses of some supplements can be harmful. This result says nothing about whether a dose "
        "is safe for you."
    ),
    "drug_interaction": (
        "Some supplements change how medicines work. Ask your pharmacist before combining them."
    ),
}
THROTTLED = (
    "Too many checks from this account contained instructions to the system. You can start new checks "
    "again later."
)


@dataclass(frozen=True)
class Decision:
    action: Literal["refuse", "clarify", "continue"]
    code: str | None  # a REFUSALS or CLARIFICATIONS key; None to continue
    sensitive_topics: tuple[str, ...]  # in SAFETY_NOTICES order
    instructions_found: bool

    @property
    def message(self) -> str | None:
        return REFUSALS.get(self.code) or CLARIFICATIONS.get(self.code)


def _pattern(entry: str) -> str:
    """A word list entry: a whole word or phrase, or a word start when it ends with `*`."""
    if entry.endswith("*"):
        return r"\b" + re.escape(entry[:-1])
    return r"\b" + re.escape(entry) + r"\b"


def keyword_topics(text: str, keywords: dict[str, list[str]]) -> set[str]:
    """The safety topics whose word list matches the text (case-insensitive)."""
    lowered = text.lower()
    return {
        topic
        for topic, entries in keywords.items()
        if any(re.search(_pattern(e.lower()), lowered) for e in entries)
    }


def ordered_topics(topics: set[str]) -> tuple[str, ...]:
    return tuple(t for t in SAFETY_NOTICES if t in topics)


def decide(
    extraction: TriagedExtraction, input_text: str, guardrails: dict
) -> Decision:
    triage = extraction.triage
    topics = ordered_topics(
        set(triage.sensitive_topics)
        | keyword_topics(input_text, guardrails["sensitive_keywords"])
    )

    def made(action, code=None):
        return Decision(action, code, topics, triage.instructions_found)

    if triage.request != "claim_check":
        return made("refuse", f"refuse_{triage.request}")
    if not extraction.claims:
        return made("clarify", "clarify_no_claim")
    if not any(claim.specific for claim in extraction.claims):
        return made("clarify", "clarify_vague")
    return made("continue")


def claim_topics(claim: dict, guardrails: dict) -> set[str]:
    """Safety topics in the claim as the user confirmed it: an edit can add a population."""
    text = " ".join(str(v) for v in claim.values() if v)
    return keyword_topics(text, guardrails["sensitive_keywords"])


def notices(topics: tuple[str, ...] | list[str]) -> list[dict]:
    return [
        {"topic": t, "text": SAFETY_NOTICES[t]} for t in ordered_topics(set(topics))
    ]
