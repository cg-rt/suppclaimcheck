"""P1.04 and P1.10 unit tests that need no database: hashing, CSRF, redirects, headers, page wording."""

from fastapi.testclient import TestClient

from scc_web import config, views
from scc_web.auth import passwords
from scc_web.context import Deps, safe_next
from scc_web.core_client import CoreClient
from scc_web.main import build_app
from scc_web.security import SESSION_COOKIE, set_session_cookie, tokens_match
from tests.web_samples import RESULT, job

CFG = config.load()


def test_hash_is_argon2id_and_verifies_only_the_right_password():
    stored = passwords.hash_password("correct horse battery")
    assert stored.startswith("$argon2id$")
    assert passwords.verify(stored, "correct horse battery")
    assert not passwords.verify(stored, "correct horse batterY")
    assert not passwords.needs_rehash(stored)


def test_unknown_account_and_broken_hash_never_verify():
    assert not passwords.verify(None, "anything at all")
    assert not passwords.verify("not-a-hash", "anything at all")


def test_new_password_rules_come_from_config():
    assert passwords.check_new_password("short", CFG) == "Use at least 12 characters."
    assert passwords.check_new_password("x" * 129, CFG) == "Use at most 128 characters."
    assert passwords.check_new_password("twelve chars", CFG) is None


def test_csrf_tokens_must_both_exist_and_match():
    assert tokens_match("abc", "abc")
    assert not tokens_match("abc", "abd")
    assert not tokens_match(None, "abc")
    assert not tokens_match("", "")


def test_next_path_stays_on_this_site():
    assert safe_next("/history") == "/history"
    for bad in ("https://evil.example", "//evil.example", "/\\evil.example", None, ""):
        assert safe_next(bad) == "/checks/new"


def test_session_cookie_has_the_host_prefix_rules():
    from fastapi.responses import Response

    response = Response()
    set_session_cookie(response, "id", 3600)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=id")
    for part in ("HttpOnly", "Secure", "Path=/", "SameSite=lax"):
        assert part in cookie
    assert "Domain" not in cookie


def _app():
    # The sign-in form and redirects need no database while nobody is signed in.
    deps = Deps(pool=None, core=CoreClient("http://core"), config=CFG)
    return build_app({"database": lambda: None}, deps=deps)


def test_sign_in_page_sets_headers_and_a_pre_session_csrf_cookie():
    response = TestClient(_app(), base_url="https://web").get("/sign-in")
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "__Host-scc_csrf=" in response.headers["set-cookie"]
    assert 'name="csrf_token"' in response.text


def test_signed_out_pages_redirect_to_sign_in_with_the_way_back():
    client = TestClient(_app(), base_url="https://web", follow_redirects=False)
    response = client.get("/history")
    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in?next=/history"


def test_static_files_are_cacheable_and_fonts_are_self_hosted():
    client = TestClient(_app(), base_url="https://web")
    css = client.get("/static/css/app.css")
    assert css.status_code == 200
    assert "no-store" not in css.headers.get("cache-control", "")
    assert "fonts.googleapis" not in css.text
    assert (
        client.get(
            "/static/fonts/nunito-sans-5.3.0-latin-wght-normal.woff2"
        ).status_code
        == 200
    )


def test_explanations_never_overstate():
    assert (
        views.explain(RESULT)
        == "2 passages in the 1 article read support the claim, and none contradict it."
    )
    mixed = {**RESULT, "verdict": "Mixed", "supporting": 1, "contradicting": 1}
    assert views.explain(mixed).startswith(
        "The evidence points both ways: 1 passage support"
    )
    nothing = {**RESULT, "verdict": "Not found", "articles_assessed": 0}
    assert "No open-access article" in views.explain(nothing)
    for verdict in (
        "Supported",
        "Contradicted",
        "Mixed",
        "Insufficient evidence",
        "Not found",
    ):
        text = views.explain({**RESULT, "verdict": verdict}).lower()
        assert "proven" not in text and "cures" not in text


def test_score_steps_show_the_rubric_arithmetic():
    assert views.score_steps(RESULT)[-1] == "Score: 50 + 50 × +1.00 × 0.33 = 67"
    assert (
        "stays at 50"
        in views.score_steps({**RESULT, "supporting": 0, "contradicting": 0})[0]
    )


def test_result_view_splits_quotes_and_labels_licences():
    view = views.result_view(job(1))
    assert [q["section"] for q in view["supporting"]] == ["Results", "Discussion"]
    assert view["contradicting"] == []
    assert view["articles"][0]["licence"] == "CC BY"
    assert view["skipped_total"] == 9


def test_progress_marks_done_current_and_waiting():
    events = [{"state": "searching", "at": "2026-10-24T10:00:05+00:00"}]
    rows = views.progress("retrieving", events)
    assert [r["status"] for r in rows] == [
        "done",
        "current",
        "waiting",
        "waiting",
        "waiting",
        "waiting",
    ]
    assert rows[0]["at"] == "10:00:05"
    assert all(r["status"] == "done" for r in views.progress("done", events))
