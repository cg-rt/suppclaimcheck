"""P1.10 acceptance: a browser smoke test of the signed-in pages, with axe accessibility checks.

Runs against a running web module, read-only (it never starts a check, so it spends nothing):
    SCC_E2E_URL=http://127.0.0.1:18080 SCC_E2E_EMAIL=... SCC_E2E_PASSWORD=... uv run pytest tests/e2e
Skipped when SCC_E2E_URL is not set. Needs `uv run playwright install chromium-headless-shell` once.
The account should have at least one finished check, so the result page is covered too.
"""

import os

import pytest

URL = os.environ.get("SCC_E2E_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(
    not URL, reason="SCC_E2E_URL not set (no running web module)"
)

WIDTHS = [1440, 390, 320]


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{URL}/sign-in")
        page.get_by_label("Email").fill(os.environ["SCC_E2E_EMAIL"])
        page.get_by_label("Password").fill(os.environ["SCC_E2E_PASSWORD"])
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url("**/checks/new")
        yield page
        browser.close()


def serious_violations(page) -> list[str]:
    from axe_playwright_python.sync_playwright import Axe

    violations = Axe().run(page).response["violations"]
    return [
        f"{v['id']}: {v['help']}"
        for v in violations
        if v["impact"] in {"serious", "critical"}
    ]


def check(page, path: str, expected: str) -> None:
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(f"{URL}{path}")
        assert expected in page.content(), f"{path} at {width}px"
        assert page.evaluate("document.documentElement.scrollWidth") <= width, (
            f"{path} scrolls sideways at {width}px"
        )
        assert serious_violations(page) == [], f"{path} at {width}px"


def test_new_check_page(page):
    check(page, "/checks/new", "What claim do you want to check?")


def test_history_and_the_latest_result(page):
    check(page, "/history", "Your checks")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{URL}/history")
    finished = page.locator(".history__item", has=page.locator(".verdict-ink")).first
    if finished.count() == 0:
        pytest.skip("this account has no finished check")
    href = finished.locator("a").first.get_attribute("href")
    check(page, href, "Not medical advice")


def test_account_page(page):
    check(page, "/account", "Change password")


def test_sign_out_ends_the_session(page):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{URL}/history")
    page.get_by_role("button", name="Sign out").click()
    page.wait_for_url("**/sign-in")
    assert serious_violations(page) == []
    page.goto(f"{URL}/history")
    assert "/sign-in" in page.url
