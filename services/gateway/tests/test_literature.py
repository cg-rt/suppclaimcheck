"""P1.07 acceptance: an article whose licence is outside the allowlist is skipped and counted, never fetched."""

import asyncio

import httpx

from scc_gateway import config
from scc_gateway.literature import OPEN_ACCESS_FILTER, Literature, RateLimiter


def record(pmid: str, pmcid: str | None, license: str | None) -> dict:
    return {
        "pmid": pmid,
        "pmcid": pmcid,
        "license": license,
        "inEPMC": "Y",
        "isOpenAccess": "Y",
        "title": f"T{pmid}",
        "pubYear": "2024",
    }


def fake_sources(records: list[dict], seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "esearch.fcgi" in request.url.path:
            return httpx.Response(
                200, json={"esearchresult": {"count": "3", "idlist": ["1", "2", "3"]}}
            )
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"resultList": {"result": records}})
        if request.url.path.endswith("/fullTextXML"):
            return httpx.Response(200, text="<article/>")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_disallowed_licences_are_skipped_and_counted():
    seen: list[str] = []
    records = [
        record("1", "PMC1", "cc by-nc-nd"),
        record("2", "PMC2", "all rights reserved"),
        record("3", "PMC3", None),
    ]
    lit = Literature(config.load(), None, None, fake_sources(records, seen))
    result = asyncio.run(lit.fetch(["1", "2", "3", "4"], full_texts=5))
    assert list(result.full_texts) == ["PMC1"]
    assert result.counts == {
        "not_in_europe_pmc": 1,
        "licence_not_allowed": 2,
        "no_full_text": 0,
    }
    assert [a.get("skip_reason") for a in result.articles] == [
        None,
        "licence_not_allowed",
        "licence_not_allowed",
    ]
    # Full text was requested for the allowed article only.
    assert [u for u in seen if "fullTextXML" in u] == [
        "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1/fullTextXML"
    ]


def test_search_adds_the_open_access_filter_and_identifies_the_tool():
    seen: list[str] = []
    lit = Literature(config.load(), None, None, fake_sources([], seen))
    result = asyncio.run(lit.search('"creatine"[tiab]', 5))
    assert result["pmids"] == ["1", "2", "3"]
    url = httpx.URL(seen[0])
    assert url.params["term"] == f'("creatine"[tiab]) AND {OPEN_ACCESS_FILTER}'
    assert url.params["tool"] == "suppclaimcheck" and "api_key" not in url.params


def test_rate_limiter_spaces_requests():
    async def three():
        limiter = RateLimiter(per_second=20)
        loop = asyncio.get_running_loop()
        start = loop.time()
        for _ in range(3):
            await limiter.wait()
        return loop.time() - start

    assert (
        asyncio.run(three()) >= 0.09
    )  # 3 requests at 20/s need at least two 50 ms gaps
