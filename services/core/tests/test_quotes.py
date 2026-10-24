"""Quote verification (P1.09): typesetting differences pass, changed words never do."""

import pytest

from scc_core.pipeline.quotes import normalise, verify_quote

PASSAGE = (
    "Creatine supplementation combined with resistance training increased upper‑body "
    "strength (SMD = 0.35; 95% CI 0.12–0.58) in adults aged ≥60 years. "
    "These ﬁndings were “consistent” across sensitivity analyses."
)
MIN = 20


@pytest.mark.parametrize(
    "quote",
    [
        # exact
        "increased upper‑body strength",
        # straight hyphen and normal spaces for the typeset ones
        "increased upper-body strength (SMD = 0.35; 95% CI 0.12-0.58)",
        # line break inside the quote
        "Creatine supplementation combined\n   with resistance training",
        # ligature written out, straight quotes for curly ones
        'These findings were "consistent" across',
    ],
)
def test_typesetting_differences_pass(quote):
    assert verify_quote(quote, PASSAGE, MIN).verified


@pytest.mark.parametrize(
    "quote",
    [
        # one word changed
        "increased lower-body strength (SMD = 0.35",
        # one word added
        "Creatine supplementation combined with heavy resistance training",
        # a number changed
        "strength (SMD = 0.53; 95% CI 0.12-0.58)",
        # words skipped with an ellipsis
        "Creatine supplementation ... increased upper-body strength",
        # different capitals
        "creatine supplementation combined with resistance",
        # spans text that isn't there
        "These findings were consistent across all outcomes",
    ],
)
def test_near_misses_fail(quote):
    result = verify_quote(quote, PASSAGE, MIN)
    assert not result.verified
    assert result.reason == "not_in_passage"


def test_short_quotes_fail_even_if_present():
    result = verify_quote("Creatine", PASSAGE, MIN)
    assert (result.verified, result.reason) == (False, "too_short")


def test_normalise_rules():
    assert normalise("a­b\u200bc") == "abc"
    assert normalise("‘x’ “y”") == "'x' \"y\""
    assert normalise("1–2 3—4 5−6") == "1-2 3-4 5-6"
    assert normalise("  a \t\n b  ") == "a b"
    assert normalise("ﬁt") == "fit"
