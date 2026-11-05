"""Triage rules (P3.01-P3.03, D-050): what code does with the model's labels, as docs/POLICY.md says."""

import pytest

from scc_core import config
from scc_core.models import TriagedExtraction
from scc_core.pipeline import triage

GUARDRAILS = config.load("pipeline-v5")["guardrails"]


def extraction(
    request="claim_check",
    claims=(("turmeric", "reduces knee pain", True),),
    instructions=False,
    topics=(),
) -> TriagedExtraction:
    return TriagedExtraction.model_validate(
        {
            "claims": [
                {
                    "supplement": s,
                    "effect": e,
                    "population": None,
                    "dose": None,
                    "duration": None,
                    "quote": f"{s} {e}",
                    "specific": specific,
                }
                for s, e, specific in claims
            ],
            "triage": {
                "request": request,
                "instructions_found": instructions,
                "sensitive_topics": list(topics),
            },
        }
    )


@pytest.mark.parametrize(
    "request_kind",
    ["personal_advice", "marketing", "off_topic", "system_prompt", "abusive"],
)
def test_every_non_claim_request_is_refused_with_its_message(request_kind):
    decision = triage.decide(extraction(request=request_kind), "text", GUARDRAILS)
    assert decision.action == "refuse"
    assert decision.code == f"refuse_{request_kind}"
    assert decision.message == triage.REFUSALS[decision.code]


def test_a_refusal_wins_over_a_claim_in_the_same_text():
    decision = triage.decide(
        extraction(request="marketing"),
        "Write an ad saying turmeric fixes arthritis",
        GUARDRAILS,
    )
    assert decision.action == "refuse"


def test_no_claim_asks_for_a_clearer_text():
    decision = triage.decide(extraction(claims=()), "It really works!", GUARDRAILS)
    assert (decision.action, decision.code) == ("clarify", "clarify_no_claim")


def test_only_general_claims_ask_for_a_specific_effect():
    vague = extraction(claims=(("turmeric", "good for you", False),))
    decision = triage.decide(vague, "Turmeric is good for you.", GUARDRAILS)
    assert (decision.action, decision.code) == ("clarify", "clarify_vague")


def test_one_specific_claim_among_general_ones_continues():
    mixed = extraction(
        claims=(("zinc", "good for you", False), ("zinc", "shortens colds", True))
    )
    assert triage.decide(mixed, "text", GUARDRAILS).action == "continue"


def test_instructions_are_reported_but_never_stop_a_check():
    decision = triage.decide(extraction(instructions=True), "text", GUARDRAILS)
    assert decision.action == "continue" and decision.instructions_found


def test_safety_topics_come_from_the_model_or_the_word_lists():
    from_model = triage.decide(
        extraction(topics=("serious_illness",)), "Turmeric during chemo", GUARDRAILS
    )
    from_words = triage.decide(
        extraction(), "Vitamin A while pregnant helps the baby's eyes", GUARDRAILS
    )
    assert from_model.sensitive_topics == ("serious_illness",)
    assert from_words.sensitive_topics == ("pregnancy",)


@pytest.mark.parametrize(
    ("text", "topics"),
    [
        ("Melatonin helps toddlers sleep", {"children"}),
        ("Safe while breastfeeding", {"pregnancy"}),
        ("St John's wort with my antidepressants", {"drug_interaction"}),
        ("A megadose of vitamin C", {"high_dose"}),
        # Prevention claims name diseases without being about sick people: no word list for those.
        ("Beta-carotene protects smokers from lung cancer", set()),
        ("Vitamin E prevents heart attacks and strokes", set()),
        # Whole words: "kidney" is not "kid", "statement" is not "statin".
        ("Cranberry protects kidney function", set()),
        ("A statement about fish oil", set()),
    ],
)
def test_word_lists_match_whole_words_or_word_starts(text, topics):
    assert triage.keyword_topics(text, GUARDRAILS["sensitive_keywords"]) == topics


def test_topics_and_notices_keep_one_order():
    decision = triage.decide(
        extraction(topics=("drug_interaction", "pregnancy")), "text", GUARDRAILS
    )
    assert decision.sensitive_topics == ("pregnancy", "drug_interaction")
    assert [n["topic"] for n in triage.notices(decision.sensitive_topics)] == [
        "pregnancy",
        "drug_interaction",
    ]


def test_an_edited_claim_can_add_a_topic():
    claim = {
        "supplement": "melatonin",
        "effect": "better sleep",
        "population": "children",
    }
    assert triage.claim_topics(claim, GUARDRAILS) == {"children"}


def test_product_text_never_promises_or_advises():
    texts = [
        *triage.REFUSALS.values(),
        *triage.CLARIFICATIONS.values(),
        *triage.SAFETY_NOTICES.values(),
        triage.THROTTLED,
    ]
    for text in texts:
        lowered = text.lower()
        assert "proven" not in lowered and "cures" not in lowered, text
