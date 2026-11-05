"""P3.12 (D-055): a check that starts from a link to a web page.

- core-api: the [inputs] switch decides whether a link check can start, and GET /inputs says so.
- The pipeline: the gateway reads the page, core stores what the model read, and the page's own
  language (advice, sales, instructions) is the page's, not the user's.
- web: the link form, its checks, and the pages that show which page was read.
"""

import tomllib
import uuid
from pathlib import Path

import httpx
from procrastinate import App
from procrastinate.testing import InMemoryConnector
from psycopg_pool import AsyncConnectionPool

from scc_core import api as core_api
from scc_core import config as core_config
from scc_core import jobs, tasks
from scc_core.gateway_client import GatewayError
from scc_core.models import JobCreate
from scc_core.pipeline import run
from scc_web import pages as web_pages
from tests.helpers import run_async
from tests.integration.test_guardrails import claim, reply
from tests.integration.test_web_auth import sign_in, token, with_web
from tests.web_samples import job as sample_job

V9 = core_config.load("pipeline-v9")
LINK = "https://news.example.com/2026/fish-oil"
PAGE = {
    "url": LINK,
    "final_url": "https://news.example.com/2026/fish-oil-review",
    "title": "Fish oil lowers triglycerides, review finds",
    "site_name": "Example Health",
    "text": "Fish oil lowers triglycerides\n\nTaking fish oil lowers blood triglycerides in adults.",
    "truncated": False,
}


class PageGateway:
    """A fake gateway: one page (or a refusal code) and the model replies each test needs."""

    def __init__(self, page=PAGE, extraction=None, refusal: str | None = None):
        self.page_reply, self.extraction, self.refusal = page, extraction, refusal
        self.pages: list[str] = []
        self.calls: list[dict] = []
        self.alerts: list[tuple] = []

    async def page(self, url):
        self.pages.append(url)
        if self.refusal:
            raise GatewayError(422, self.refusal, "refused")
        return self.page_reply

    async def structured(self, **call):
        self.calls.append(call)
        return {"output": self.extraction or reply()}

    async def spell(self, name):
        return {"suggestion": None}

    async def notify(self, event, text, code=None):
        self.alerts.append((event, text, code))
        return {"outcome": "sent"}


def extract_link(migrated_db, gateway, link=LINK, user_id=40, config=V9):
    async def scenario():
        pool = AsyncConnectionPool(migrated_db.conninfo("core"), open=False)
        await pool.open()
        try:
            async with pool.connection() as conn:
                created, _ = await jobs.create(
                    conn,
                    JobCreate(user_id=user_id, input_type="url", input_text=link),
                    f"link-{uuid.uuid4()}",
                    config["version"],
                    None,
                )
            await run.extract(run.Runtime(pool, gateway, config), created.id)
            async with pool.connection() as conn:
                done = await jobs.get(conn, created.id)
                events = await jobs.events(conn, created.id)
                cur = await conn.execute(
                    "SELECT source_text FROM core.jobs WHERE id = %s", (created.id,)
                )
                text = (await cur.fetchone())[0]
                cur = await conn.execute(
                    "SELECT source FROM core.injection_events WHERE job_id = %s",
                    (created.id,),
                )
                injections = [r[0] for r in await cur.fetchall()]
                checks = await jobs.injection_checks(conn, user_id, 24)
            return done, events, text, injections, checks
        finally:
            await pool.close()

    return run_async(scenario())


def test_a_link_is_read_and_its_claims_wait_for_the_user(migrated_db):
    gateway = PageGateway(
        extraction=reply(claims=[claim("fish oil", "lowers triglycerides")])
    )
    job, events, text, _, _ = extract_link(migrated_db, gateway)
    assert gateway.pages == [LINK]
    assert job.state == "awaiting_confirmation"
    assert job.source == {
        "url": LINK,
        "final_url": PAGE["final_url"],
        "title": PAGE["title"],
        "site_name": "Example Health",
        "chars": len(PAGE["text"]),
        "chars_read": len(PAGE["text"]),
        "truncated": False,
    }
    assert text == PAGE["text"]
    # The model read the page with the page prompt, recorded as the extract stage (the daily quota).
    (call,) = gateway.calls
    assert call["stage"] == "extract" and call["data"] == PAGE["text"]
    assert call["instructions"] == V9["prompts"]["extract_page"]
    assert call["settings"] == V9["llm"]["extract_page"]
    assert [e.state for e in events] == [
        "submitted",
        "extracting",
        "awaiting_confirmation",
    ]


def test_a_long_page_is_cut_and_says_so(migrated_db):
    long_page = {
        **PAGE,
        "text": "\n\n".join(f"Paragraph {n}. " + "x" * 990 for n in range(40)),
    }
    gateway = PageGateway(page=long_page)
    job, _, text, _, _ = extract_link(migrated_db, gateway)
    assert job.source["truncated"] and job.source["chars"] > 30_000
    assert len(text) == job.source["chars_read"] <= V9["inputs"]["url_max_chars"]
    assert gateway.calls[0]["data"] == text


def test_a_page_that_cannot_be_read_ends_the_check_with_its_reason(migrated_db):
    gateway = PageGateway(refusal="robots_disallowed")
    job, events, _, _, _ = extract_link(migrated_db, gateway)
    assert job.state == "failed" and events[-1].detail == "robots_disallowed"
    assert job.error == run.PAGE_MESSAGES["robots_disallowed"]
    assert gateway.calls == [], "nothing spent"
    assert gateway.alerts == [
        ("job_failed", f"Job {job.id} stopped.", "robots_disallowed")
    ]
    _, events, _, _, _ = extract_link(migrated_db, PageGateway(refusal="something_new"))
    assert events[-1].detail == "page_unavailable"


def test_a_page_that_sells_or_advises_is_still_checked(migrated_db):
    gateway = PageGateway(extraction=reply(request="marketing"))
    job, _, _, _, _ = extract_link(migrated_db, gateway)
    assert job.state == "awaiting_confirmation"


def test_instructions_on_a_page_are_the_pages_not_the_users(migrated_db):
    gateway = PageGateway(extraction=reply(instructions=True))
    job, _, _, injections, checks = extract_link(migrated_db, gateway, user_id=41)
    assert job.state == "awaiting_confirmation", "ignored, not refused"
    assert injections == ["submitted_page"]
    assert checks == 0, "they don't count towards the account's limit"


def test_page_safety_words_come_from_the_claims_not_the_whole_page(migrated_db):
    page = {
        **PAGE,
        "text": PAGE["text"] + "\n\nRelated: vitamins for children and pregnancy.",
    }
    job, _, _, _, _ = extract_link(migrated_db, PageGateway(page=page))
    assert job.sensitive_topics == [] and not job.needs_review


def test_no_claim_on_a_page(migrated_db):
    job, events, _, _, _ = extract_link(
        migrated_db, PageGateway(extraction=reply(claims=[]))
    )
    assert job.state == "needs_clarification"
    assert events[-1].detail == "clarify_no_claim_page"


def with_core(migrated_db, steps, version):
    async def scenario():
        pool = AsyncConnectionPool(migrated_db.conninfo("core"), open=False)
        await pool.open()
        queue = App(connector=InMemoryConnector())
        queue.add_tasks_from(tasks.blueprint, namespace="core")
        app = core_api.build_app(checks={}, deps=core_api.Deps(pool, queue, version))
        try:
            async with (
                queue.open_async(),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://core"
                ) as client,
            ):
                return await steps(client)
        finally:
            await pool.close()

    return run_async(scenario())


def test_the_switch_decides_whether_a_link_check_can_start(migrated_db):
    async def steps(client):
        inputs = await client.get("/inputs")
        made = await client.post(
            "/jobs",
            json={"user_id": 42, "input_type": "url", "input_text": LINK},
            headers={"Idempotency-Key": "link-switch-01"},
        )
        bad = await client.post(
            "/jobs",
            json={"user_id": 42, "input_type": "url", "input_text": "not a link"},
            headers={"Idempotency-Key": "link-switch-02"},
        )
        return inputs.json(), made, bad

    inputs, made, bad = with_core(migrated_db, steps, "pipeline-v9")
    assert inputs == {"text": True, "url": True}
    assert made.status_code == 201 and made.json()["input_type"] == "url"
    assert bad.status_code == 422
    inputs, made, _ = with_core(migrated_db, steps, "pipeline-v8")
    assert inputs == {"text": True, "url": False}
    assert made.status_code == 409 and made.json()["detail"]["code"] == "input_disabled"


def test_web_offers_links_only_when_core_does(migrated_db):
    async def steps(client, ctx):
        await sign_in(client)
        on = await client.get("/checks/new")
        link_form = await client.get("/checks/new?from=link")
        ctx["core"].links = False
        off = await client.get("/checks/new?from=link")
        return on, link_form, off

    on, link_form, off = with_web(migrated_db, steps)
    assert 'aria-current="page">Text</a>' in on.text and "A link to a page" in on.text
    assert (
        'name="input_url"' in link_form.text
        and 'name="input_text"' not in link_form.text
    )
    assert "A link to a page" not in off.text and 'name="input_text"' in off.text


def test_web_sends_a_link_and_adds_https_when_missing(migrated_db):
    async def steps(client, ctx):
        await sign_in(client)
        page = await client.get("/checks/new?from=link")
        made = await client.post(
            "/checks",
            data={
                "csrf_token": token(page.text),
                "idempotency_key": "k-link-1",
                "input_type": "url",
                "input_url": "news.example.com/2026/fish-oil",
            },
        )
        wrong = await client.post(
            "/checks",
            data={
                "csrf_token": token(page.text),
                "input_type": "url",
                "input_url": "fish oil is great",
            },
        )
        ctx["core"].links = False
        off = await client.post(
            "/checks",
            data={
                "csrf_token": token(page.text),
                "input_type": "url",
                "input_url": LINK,
            },
        )
        return made, wrong, off, ctx["core"].calls

    made, wrong, off, calls = with_web(migrated_db, steps)
    assert made.status_code == 303
    sent = [body for method, path, body in calls if (method, path) == ("POST", "/jobs")]
    assert sent[0]["input_type"] == "url" and sent[0]["input_text"] == LINK
    assert wrong.status_code == 400 and "isn&#39;t a web address" in wrong.text
    assert 'value="fish oil is great"' in wrong.text, (
        "what was typed stays in the field"
    )
    assert off.status_code == 409 and "Links are switched off." in off.text


def test_web_pages_name_the_page_that_was_read(migrated_db):
    source = {
        "url": LINK, "final_url": PAGE["final_url"], "title": PAGE["title"],
        "site_name": "Example Health", "chars": 40_000, "chars_read": 30_000, "truncated": True,
    }  # fmt: skip

    async def steps(client, ctx):
        await sign_in(client)
        uid = ctx["user"]
        core = ctx["core"]
        waiting = core.add(
            sample_job(uid, "extracting", input_type="url", input_text=LINK)
        )
        confirm = core.add(
            sample_job(
                uid,
                "awaiting_confirmation",
                input_type="url",
                input_text=LINK,
                source=source,
            )
        )
        done = core.add(
            sample_job(uid, "done", input_type="url", input_text=LINK, source=source)
        )
        failed = core.add(
            sample_job(
                uid,
                "failed",
                input_type="url",
                input_text=LINK,
                error=run.PAGE_MESSAGES["not_html"],
            )
        )
        return [
            (await client.get(f"/checks/{j['id']}")).text
            for j in (waiting, confirm, done, failed)
        ] + [(await client.get("/history")).text]

    progress, confirm, result, stopped, history = with_web(migrated_db, steps)
    assert (
        "Reading the page for claims" in progress and "Received your link" in progress
    )
    assert (
        "These are the claims found on the page." in confirm
        and "From the page:" in confirm
    )
    assert f'href="{PAGE["final_url"]}" rel="nofollow noopener noreferrer"' in confirm
    assert (
        "Your link led here:" in confirm
        and 'first <span class="num">30,000</span> characters' in confirm
    )
    assert "Found on <a" in result and PAGE["title"] in result
    assert "isn&#39;t a web page" in stopped and "Paste the text instead" in stopped
    assert "Try another link" in stopped
    assert PAGE["title"] in history


def test_the_reader_page_is_public_and_matches_the_gateway_config(migrated_db):
    async def steps(client, ctx):
        return await client.get("/reader")

    page = with_web(migrated_db, steps)
    assert page.status_code == 200 and "Sign out" not in page.text
    gateway = tomllib.loads(
        Path("services/gateway/src/scc_gateway/config/gateway-v3.toml").read_text(
            "utf-8"
        )
    )["pages"]
    reader = web_pages.READER
    assert reader["user_agent"] == gateway["user_agent"]
    assert reader["token"] == gateway["robots_token"]
    assert reader["timeout_s"] == gateway["timeout_s"]
    assert reader["max_mb"] * 1_000_000 == gateway["max_bytes"]
    assert reader["min_interval_s"] == gateway["min_interval_s"]
    assert reader["robots_cache_minutes"] == gateway["robots_cache_minutes"]
    assert gateway["user_agent"].endswith("(+https://suppclaimcheck.com/reader)")
    assert f"User-agent: {gateway['robots_token']}" in page.text
