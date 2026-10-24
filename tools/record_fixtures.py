"""Records the literature fixtures for the canned claim (P1.07, P1.08). Makes live calls: run by hand only.

It runs the gateway's own literature code against PubMed and Europe PMC with a recording transport,
so the fixtures are exactly the requests the pipeline makes. CI replays them and never calls out.
Without an NCBI key it stays at 3 requests per second; it makes three or four requests in total.

Run: uv run python tools/record_fixtures.py
"""

import asyncio
import json
import sys
from pathlib import Path

from scc_common.recorded import RecordingTransport
from scc_core import config as core_config
from scc_core.models import Claim
from scc_core.pipeline import query
from scc_gateway import config as gateway_config
from scc_gateway.literature import Literature

ROOT = Path(__file__).resolve().parent.parent
FOLDER = ROOT / "tests" / "fixtures" / "canned_claim" / "literature"
# The canned claim, as a user would confirm it after extraction.
CLAIM = Claim(
    supplement="creatine",
    effect="increases muscle strength when combined with resistance training",
    population="older adults",
)


async def main() -> int:
    cfg = core_config.load()
    transport = RecordingTransport(FOLDER)
    literature = Literature(
        gateway_config.load(), ncbi_api_key=None, pool=None, transport=transport
    )
    found = await literature.search(
        query.build(CLAIM), cfg["search"]["pubmed_max_results"]
    )
    print(f"search: {found['count']} matches, {len(found['pmids'])} PMIDs")
    fetched = await literature.fetch(
        found["pmids"], cfg["search"]["articles_to_assess"]
    )
    print(f"fetch: {list(fetched.full_texts)} full text(s), skipped {fetched.counts}")
    await transport.aclose()
    _trim_metadata()
    return 0


# Europe PMC fields the gateway never reads. Dropping them keeps third-party abstracts out of the repo.
UNUSED_FIELDS = {"abstractText", "keywordList", "meshHeadingList", "chemicalList", "affiliation", "authorList",
                 "fullTextUrlList", "commentCorrectionList", "grantsList", "subsetList", "dbCrossReferenceList"}  # fmt: skip


def _trim_metadata() -> None:
    index = json.loads((FOLDER / "index.json").read_text("utf-8"))
    for key, entry in index.items():
        if "/rest/search?" not in key:
            continue
        path = FOLDER / entry["file"]
        data = json.loads(path.read_text("utf-8"))
        for record in data.get("resultList", {}).get("result", []):
            for name in UNUSED_FIELDS:
                record.pop(name, None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
