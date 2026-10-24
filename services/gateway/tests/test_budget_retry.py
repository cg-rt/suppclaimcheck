"""P1.06: cost calculation from the price table, and the retry policy (transient errors only)."""

import asyncio
import random

import pytest

from scc_gateway import budget, config
from scc_gateway.retry import Transient, with_retries

CFG = config.load()


def test_cost_from_the_price_table():
    # 100K input tokens is the most at the normal rates.
    cost = budget.cost_eur("claude-haiku-5-5", budget.Usage(100_000, 1_000_000), CFG)
    assert cost == pytest.approx(0.01 + 0.50)


def test_long_prompts_use_the_long_prompt_rates():
    cost = budget.cost_eur("claude-haiku-5-5", budget.Usage(100_001, 0), CFG)
    assert cost == pytest.approx(100_001 * 0.50 / 1_000_000)


def test_unknown_model_is_refused():
    with pytest.raises(budget.UnknownModel):
        budget.cost_eur("some-other-model", budget.Usage(1, 1), CFG)


def test_worst_case_assumes_full_output_and_overcounts_input():
    worst = budget.worst_case_eur(
        "claude-haiku-5-5", input_chars=3000, max_tokens=1000, config=CFG
    )
    assert worst == pytest.approx((1001 * 0.10 + 1000 * 0.50) / 1_000_000)


class Flaky:
    def __init__(self, failures: list[Exception]):
        self.failures = failures
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "ok"


def run(call, attempts=3):
    delays: list[float] = []

    async def sleep(seconds):
        delays.append(seconds)

    result = asyncio.run(
        with_retries(
            call,
            attempts=attempts,
            base_s=1.0,
            max_s=5.0,
            what="test",
            sleep=sleep,
            rng=random.Random(1),
        )
    )
    return result, delays


def test_transient_errors_are_retried_with_growing_jittered_delays():
    call = Flaky([Transient("429"), Transient("503")])
    result, delays = run(call)
    assert (result, call.calls) == ("ok", 3)
    assert 0 <= delays[0] <= 1.0 and 0 <= delays[1] <= 2.0


def test_gives_up_after_the_last_attempt():
    call = Flaky([Transient("503")] * 5)
    with pytest.raises(Transient):
        run(call, attempts=3)
    assert call.calls == 3


def test_other_errors_are_not_retried():
    call = Flaky([ValueError("400 bad request")])
    with pytest.raises(ValueError):
        run(call)
    assert call.calls == 1


def test_cap_override_can_lower_but_never_raise(monkeypatch):
    for value, expected in (("0.01", 0.01), ("1000", 15.0)):
        monkeypatch.setenv("SCC_BUDGET_CAP_OVERRIDE_EUR", value)
        config.load.cache_clear()
        assert config.load()["budget"]["live_monthly_cap_eur"] == expected
    config.load.cache_clear()
