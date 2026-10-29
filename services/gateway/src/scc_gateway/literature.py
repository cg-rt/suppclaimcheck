"""Literature sources (P1.07, D-017): PubMed search, Europe PMC licence and JATS full text.

- PubMed E-utilities with the registered tool name and email, the API key when configured, and a
  client-side limit below NCBI's rate (8/s with a key, 3/s without). The open-access filter is added here.
- Europe PMC for each article's licence and full-text XML. Articles whose licence is not on the
  allowlist are skipped and counted, never read. No PDFs.
- Metadata and full text are cached by PMID in gateway.articles, so repeat checks don't refetch.
"""

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass, field

import httpx
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from scc_gateway.retry import Transient, with_retries

log = logging.getLogger("scc.literature")

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EPMC_FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
OPEN_ACCESS_FILTER = "pubmed pmc open access[filter]"
_TAGS = re.compile(r"<[^>]+>")
_PMID = re.compile(r"^\d{1,9}$")


class LiteratureUnavailable(Exception):
    pass


class RateLimiter:
    """At most `per_second` requests start in any second, across all concurrent callers."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            await asyncio.sleep(delay)


@dataclass
class FetchResult:
    articles: list[dict] = field(default_factory=list)
    full_texts: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    return " ".join(_TAGS.sub("", html.unescape(text)).split())


def _metadata(record: dict) -> dict:
    journal = (record.get("journalInfo") or {}).get("journal") or {}
    pmcid = record.get("pmcid")
    pmid = record.get("pmid")
    return {
        "pmid": pmid,
        "pmcid": pmcid,
        "title": _clean(record.get("title")),
        "authors": _clean(record.get("authorString")),
        "journal": journal.get("title") or record.get("journalTitle"),
        "year": record.get("pubYear"),
        "doi": record.get("doi"),
        "license": (record.get("license") or "").strip().lower() or None,
        "pub_types": (record.get("pubTypeList") or {}).get("pubType", []),
        # MeSH descriptors (Humans, Animals, Cells, Cultured, ...) tell human from animal and lab
        # studies (P2.05). Articles not yet indexed in MEDLINE have none.
        "mesh": [
            m["descriptorName"]
            for m in (record.get("meshHeadingList") or {}).get("meshHeading", [])
            if m.get("descriptorName")
        ],
        "open_access": record.get("isOpenAccess") == "Y",
        "in_epmc": record.get("inEPMC") == "Y",
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
        "pmc_url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/" if pmcid else None,
    }


class Literature:
    def __init__(
        self,
        config: dict,
        ncbi_api_key: str | None,
        pool: AsyncConnectionPool | None,
        transport=None,
    ):
        self.cfg = config["literature"]
        self.api_key = ncbi_api_key
        self.pool = pool
        self.transport = transport
        rate = self.cfg[
            "ncbi_requests_per_second_with_key"
            if ncbi_api_key
            else "ncbi_requests_per_second_without_key"
        ]
        self.ncbi_limiter = RateLimiter(rate)
        self.allowlist = set(self.cfg["licence_allowlist"])

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.cfg["timeout_s"], connect=5.0),
            transport=self.transport,
            headers={"User-Agent": f"{self.cfg['tool']} ({self.cfg['email']})"},
        )

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict | None = None,
        *,
        limiter=None,
        allow_404=False,
    ):
        async def once():
            if limiter:
                await limiter.wait()
            try:
                response = await client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise Transient(type(exc).__name__) from exc
            if response.status_code == 429 or response.status_code >= 500:
                raise Transient(f"status {response.status_code}")
            if response.status_code == 404 and allow_404:
                return None
            if response.status_code >= 400:
                raise LiteratureUnavailable(f"{url} answered {response.status_code}")
            return response

        try:
            return await with_retries(
                once,
                attempts=self.cfg["max_attempts"],
                base_s=self.cfg["backoff_base_s"],
                max_s=self.cfg["backoff_max_s"],
                what=url,
            )
        except Transient as exc:
            raise LiteratureUnavailable(f"{url}: {exc}") from exc

    async def search(self, query: str, max_results: int) -> dict:
        params = {
            "db": "pubmed",
            "term": f"({query}) AND {OPEN_ACCESS_FILTER}",
            "retmode": "json",
            "retmax": max_results,
            "sort": "relevance",
            "tool": self.cfg["tool"],
            "email": self.cfg["email"],
        }
        if self.api_key:
            params["api_key"] = self.api_key
        async with self._client() as client:
            response = await self._get(
                client, ESEARCH, params, limiter=self.ncbi_limiter
            )
        result = response.json()["esearchresult"]
        return {
            "query": params["term"],
            "count": int(result.get("count", 0)),
            "pmids": [p for p in result.get("idlist", []) if _PMID.match(p)],
            "query_translation": result.get("querytranslation"),
        }

    async def _cached(self, pmids: list[str]) -> dict[str, dict]:
        if self.pool is None or not pmids:
            return {}
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT pmid, metadata, fulltext_xml FROM gateway.articles WHERE pmid = ANY(%s)",
                (pmids,),
            )
            return {
                pmid: {"metadata": meta, "xml": xml}
                for pmid, meta, xml in await cur.fetchall()
            }

    async def _store(self, pmid: str, metadata: dict, xml: str | None) -> None:
        if self.pool is None:
            return
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO gateway.articles (pmid, pmcid, license, metadata, fulltext_xml)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (pmid) DO UPDATE SET metadata = EXCLUDED.metadata,
                    fulltext_xml = coalesce(EXCLUDED.fulltext_xml, gateway.articles.fulltext_xml),
                    license = EXCLUDED.license, pmcid = EXCLUDED.pmcid, fetched_at = now()
                """,
                (
                    pmid,
                    metadata.get("pmcid"),
                    metadata.get("license"),
                    Jsonb(metadata),
                    xml,
                ),
            )

    async def fetch(self, pmids: list[str], full_texts: int) -> FetchResult:
        """Metadata for every PMID, in the given order; full text for the first `full_texts` allowed ones."""
        pmids = [p for p in pmids if _PMID.match(p)]
        result = FetchResult(
            counts={"not_in_europe_pmc": 0, "licence_not_allowed": 0, "no_full_text": 0}
        )
        cached = await self._cached(pmids)
        # Metadata cached before MeSH was kept is fetched again; the cached full text is still used.
        missing = [
            p for p in pmids if p not in cached or "mesh" not in cached[p]["metadata"]
        ]
        records: dict[str, dict] = {
            p: c["metadata"] for p, c in cached.items() if p not in missing
        }
        async with self._client() as client:
            if missing:
                params = {
                    # The brackets matter: without them Europe PMC applies SRC:MED to the last ID only.
                    "query": "("
                    + " OR ".join(f"EXT_ID:{p}" for p in missing)
                    + ") AND SRC:MED",
                    "resultType": "core",
                    "format": "json",
                    "pageSize": max(25, len(missing)),
                }
                response = await self._get(client, EPMC_SEARCH, params)
                for record in response.json().get("resultList", {}).get("result", []):
                    metadata = _metadata(record)
                    if metadata["pmid"] in missing:
                        records[metadata["pmid"]] = metadata
                        await self._store(metadata["pmid"], metadata, None)

            for pmid in pmids:
                metadata = records.get(pmid)
                if metadata is None:
                    result.counts["not_in_europe_pmc"] += 1
                    continue
                article = dict(metadata)
                if metadata["license"] not in self.allowlist:
                    article["skip_reason"] = "licence_not_allowed"
                    result.counts["licence_not_allowed"] += 1
                elif not metadata["pmcid"] or not metadata["in_epmc"]:
                    article["skip_reason"] = "no_full_text"
                    result.counts["no_full_text"] += 1
                elif len(result.full_texts) < full_texts:
                    xml = (cached.get(pmid) or {}).get("xml")
                    if xml is None:
                        response = await self._get(
                            client,
                            EPMC_FULLTEXT.format(pmcid=metadata["pmcid"]),
                            allow_404=True,
                        )
                        xml = response.text if response is not None else None
                        if xml:
                            await self._store(pmid, metadata, xml)
                    if xml:
                        result.full_texts[metadata["pmcid"]] = xml
                    else:
                        article["skip_reason"] = "no_full_text"
                        result.counts["no_full_text"] += 1
                result.articles.append(article)
        log.info(
            "literature fetch",
            extra={
                "pmids": len(pmids),
                "full_texts": len(result.full_texts),
                **result.counts,
            },
        )
        return result
