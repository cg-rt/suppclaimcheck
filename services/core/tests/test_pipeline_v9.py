"""pipeline-v9 (P3.12, D-055): v8 plus article links. A text check sends exactly what it sent under v8."""

import pytest
from pydantic import ValidationError

from scc_core import config
from scc_core.models import JobCreate, TriagedExtraction
from scc_core.pipeline import triage
from scc_core.pipeline.run import MESSAGES, PAGE_MESSAGES, cut_text

V9 = config.load("pipeline-v9")
GUARDRAILS = V9["guardrails"]


def test_v9_is_active():
    assert config.ACTIVE == "pipeline-v9"


def test_v9_adds_only_link_settings_to_v8():
    v8 = config.load("pipeline-v8")
    assert {
        k: v for k, v in V9.items() if k not in {"version", "inputs", "llm", "prompts"}
    } == {k: v for k, v in v8.items() if k not in {"version", "llm", "prompts"}}
    # Everything a text check uses is unchanged, so the v8 recording replays.
    assert {k: v for k, v in V9["llm"].items() if k != "extract_page"} == v8["llm"]
    assert {k: v for k, v in V9["prompts"].items() if k != "extract_page"} == v8[
        "prompts"
    ]
    assert V9["inputs"] == {"url": True, "url_max_chars": 30_000}
    assert V9["llm"]["extract_page"]["max_tokens"] == 2048
    assert "untrusted data" in V9["prompts"]["extract_page"]


def test_every_page_code_has_a_message_that_offers_another_way():
    gateway_codes = {
        "url_invalid", "url_blocked", "robots_disallowed", "ai_disallowed", "not_html",
        "too_large", "no_text", "page_not_found", "site_refused", "page_unavailable",
        "page_timeout", "pages_not_configured",
    }  # fmt: skip
    assert gateway_codes | {"input_disabled"} == set(PAGE_MESSAGES)
    for code, message in PAGE_MESSAGES.items():
        assert MESSAGES[code] == message
        assert "paste" in message.lower(), code


@pytest.mark.parametrize(
    "address",
    [
        "https://example.com/a",
        "http://news.example.org/2026/story?id=4#top",
        "HTTPS://Example.com",
    ],
)
def test_links_are_one_web_address(address):
    assert (
        JobCreate(user_id=1, input_type="url", input_text=address).input_type == "url"
    )


@pytest.mark.parametrize(
    "address",
    [
        "example.com/a",
        "ftp://example.com/a",
        "https://example.com/a b",
        "https://user@example.com/",
        "see https://example.com",
        "https://" + "a" * 2050 + ".com",
    ],
)
def test_anything_else_is_not_a_link(address):
    with pytest.raises(ValidationError):
        JobCreate(user_id=1, input_type="url", input_text=address)


def test_cut_text_keeps_whole_paragraphs():
    text = "\n\n".join(["a" * 40, "b" * 40, "c" * 40])
    assert cut_text(text, 1000) == text
    assert cut_text(text, 90) == "a" * 40 + "\n\n" + "b" * 40
    assert cut_text(text, 41) == "a" * 40
    assert cut_text("x" * 100, 30) == "x" * 30


def page(
    request="claim_check",
    claims=(("fish oil", "lowers triglycerides", True),),
    topics=(),
):
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
                "instructions_found": False,
                "sensitive_topics": list(topics),
            },
        }
    )


@pytest.mark.parametrize(
    "label", ["personal_advice", "marketing", "off_topic", "system_prompt"]
)
def test_a_page_is_checked_whatever_its_own_language(label):
    """The user asked for the page's claims: a page that sells or advises is still checked."""
    decision = triage.decide(
        page(request=label), "fish oil lowers triglycerides", GUARDRAILS, page=True
    )
    assert decision.action == "continue"
    # The same label on the user's own text still refuses.
    assert triage.decide(page(request=label), "text", GUARDRAILS).action == "refuse"


def test_an_abusive_page_is_refused():
    decision = triage.decide(page(request="abusive"), "", GUARDRAILS, page=True)
    assert (decision.action, decision.code) == ("refuse", "refuse_abusive")


def test_pages_get_their_own_clarifications():
    none = triage.decide(page(claims=()), "", GUARDRAILS, page=True)
    vague = triage.decide(
        page(claims=(("sea moss", "good for you", False),)), "", GUARDRAILS, page=True
    )
    assert (none.code, vague.code) == ("clarify_no_claim_page", "clarify_vague_page")
    assert "on that page" in none.message and "The page" in vague.message
