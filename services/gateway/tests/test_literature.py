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


def espell(corrected: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/espell.fcgi")
        assert request.url.params["db"] == "pubmed"
        tag = (
            f"<CorrectedQuery>{corrected}</CorrectedQuery>"
            if corrected
            else "<CorrectedQuery/>"
        )
        return httpx.Response(
            200,
            text=f"<eSpellResult><Query>{request.url.params['term']}</Query>{tag}</eSpellResult>",
        )

    return httpx.MockTransport(handler)


def test_spell_returns_pubmeds_correction():
    lit = Literature(config.load(), None, None, espell("ashwagandha"))
    assert asyncio.run(lit.spell("Ashwaganda")) == {
        "term": "Ashwaganda",
        "suggestion": "ashwagandha",
    }


def test_spell_returns_none_for_a_known_term():
    lit = Literature(config.load(), None, None, espell(""))
    assert asyncio.run(lit.spell("vitamin c"))["suggestion"] is None


def test_spell_ignores_a_change_of_hyphens_or_spaces():
    lit = Literature(config.load(), None, None, espell("beta carotene supplements"))
    assert asyncio.run(lit.spell("Beta-carotene supplements"))["suggestion"] is None


def test_spell_keeps_only_name_characters():
    lit = Literature(config.load(), None, None, espell("turmeric[tiab] OR x"))
    assert asyncio.run(lit.spell("turmerc"))["suggestion"] == "turmeric tiab or x"


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


def test_metadata_keeps_publication_types_and_mesh_headings():
    rec = {
        **record("1", "PMC1", "cc by"),
        "pubTypeList": {"pubType": ["Randomized Controlled Trial", "Journal Article"]},
        "meshHeadingList": {
            "meshHeading": [{"descriptorName": "Humans"}, {"descriptorName": "Adult"}]
        },
    }
    lit = Literature(config.load(), None, None, fake_sources([rec], []))
    (article,) = asyncio.run(lit.fetch(["1"], full_texts=0)).articles
    assert article["pub_types"] == ["Randomized Controlled Trial", "Journal Article"]
    assert article["mesh"] == ["Humans", "Adult"]
