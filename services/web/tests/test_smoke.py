"""P1.12: the smoke test's own logic, against a scripted web module (its real run is the deploy rehearsal)."""

import httpx
import pytest

from scc_web import smoke

SIGN_IN = '<form><input type="hidden" name="csrf_token" value="tok-1"></form>'


def scripted(version="0.2.0", deps_status="ok", sign_in_ok=True):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/health/deps":
            return httpx.Response(
                200 if deps_status == "ok" else 503, json={"status": deps_status}
            )
        if path == "/version":
            return httpx.Response(200, json={"version": version})
        if path == "/sign-in" and request.method == "GET":
            return httpx.Response(
                200,
                text=SIGN_IN,
                headers={"set-cookie": "__Host-scc_csrf=c1; Path=/; Secure; HttpOnly"},
            )
        if path == "/sign-in":
            if not sign_in_ok:
                return httpx.Response(400, text=SIGN_IN)
            return httpx.Response(
                303,
                headers=[
                    ("location", "/history"),
                    ("set-cookie", "__Host-scc_session=s1; Path=/; Secure; HttpOnly"),
                    ("set-cookie", '__Host-scc_csrf=""; Max-Age=0; Path=/; Secure'),
                ],
            )
        if path == "/history":
            ok = "__Host-scc_session=s1" in request.headers.get("cookie", "")
            return httpx.Response(200, text="<h1>Your checks</h1>" if ok else "Sign in")
        return httpx.Response(404)

    return smoke.Browser("http://web", transport=httpx.MockTransport(handle)), seen


def test_passes_and_sends_the_secure_cookies_over_plain_http(capsys):
    browser, seen = scripted()
    smoke.run(browser, "smoke@example.com", "pw", "0.2.0", claim=False)
    post = next(r for r in seen if r.method == "POST")
    assert "__Host-scc_csrf=c1" in post.headers["cookie"]
    assert b"csrf_token=tok-1" in post.content
    assert browser.cookies == {"__Host-scc_session": "s1"}, (
        "the cleared CSRF cookie is dropped"
    )
    assert capsys.readouterr().out.count("ok    ") == 4


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"deps_status": "degraded"}, "health"),
        ({"version": "0.1.0"}, "version 0.1.0"),
        ({"sign_in_ok": False}, "sign in"),
    ],
)
def test_stops_at_the_first_failed_check(kwargs, message):
    browser, _ = scripted(**kwargs)
    with pytest.raises(smoke.SmokeFailed, match=message):
        smoke.run(browser, "smoke@example.com", "pw", "0.2.0", claim=False)
