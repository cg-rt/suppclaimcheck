"""Post-deploy smoke test (P1.12): `python -m scc_web.smoke [--claim]`, run by deploy.sh as a one-off container.

Checks, against web on the internal network:
1. `/health/deps` is ok for every dependency (web's database, then core-api, which checks the gateway).
2. `/version` reports the version being deployed (SCC_EXPECT_VERSION).
3. The smoke account can sign in and open its history.
4. With --claim: one canned claim runs to a result, with at least one verified quote. This makes real
   LLM calls inside the live budget lane (about EUR 0.01).

Prints one line per check and exits non-zero at the first failure. The password comes from a secret file.
"""

import argparse
import html
import os
import re
import sys
import time

import httpx

from scc_common.db import read_secret

CLAIM = "Taking creatine while doing resistance training makes older adults stronger."
STEP_TIMEOUT_S = 180


class SmokeFailed(Exception):
    pass


class Browser:
    """A tiny client that keeps cookies by hand: the session cookie is Secure, and this runs over plain
    HTTP inside the internal network, where a normal cookie jar would refuse to send it back."""

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.client = httpx.Client(
            base_url=base_url, timeout=15, transport=transport, follow_redirects=False
        )
        self.cookies: dict[str, str] = {}

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if self.cookies:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        response = self.client.request(method, path, headers=headers, **kwargs)
        for header in response.headers.get_list("set-cookie"):
            name, _, rest = header.partition("=")
            value = rest.split(";", 1)[0]
            if "max-age=0" in header.lower() or not value or value == '""':
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        return response


def token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not match:
        raise SmokeFailed("no CSRF token on the page")
    return match.group(1)


def check(ok: bool, what: str) -> None:
    if not ok:
        raise SmokeFailed(what)
    print(f"ok    {what}", flush=True)


def wait_for(browser: Browser, path: str, done: str, sleep=time.sleep) -> str:
    deadline = time.monotonic() + STEP_TIMEOUT_S
    while time.monotonic() < deadline:
        page = browser.request("GET", path)
        if done in page.text:
            return page.text
        if "This check stopped" in page.text:
            raise SmokeFailed(f"the check stopped: {path}")
        sleep(2)
    raise SmokeFailed(f"no '{done}' on {path} after {STEP_TIMEOUT_S}s")


def run(
    browser: Browser,
    email: str,
    password: str,
    expect_version: str | None,
    claim: bool,
    sleep=time.sleep,
) -> None:
    deps = browser.request("GET", "/health/deps")
    check(
        deps.status_code == 200 and deps.json().get("status") == "ok",
        f"health: {deps.text[:200]}",
    )
    version = browser.request("GET", "/version").json().get("version")
    check(
        expect_version is None or version == expect_version,
        f"version {version} (expected {expect_version or 'any'})",
    )

    form = browser.request("GET", "/sign-in")
    signed_in = browser.request(
        "POST",
        "/sign-in",
        data={
            "email": email,
            "password": password,
            "csrf_token": token(form.text),
            "next": "/history",
        },
    )
    check(
        signed_in.status_code == 303
        and signed_in.headers.get("location") == "/history",
        "sign in",
    )
    check("Your checks" in browser.request("GET", "/history").text, "history page")
    if not claim:
        return

    new = browser.request("GET", "/checks/new")
    created = browser.request(
        "POST", "/checks", data={"input_text": CLAIM, "csrf_token": token(new.text)}
    )
    check(created.status_code == 303, "submit the canned claim")
    path = created.headers["location"]
    confirm_page = wait_for(browser, path, "Is this the claim to check?", sleep)
    fields = {}
    for name in ("supplement", "population", "dose", "duration"):
        match = re.search(rf'name="{name}"[^>]*value="([^"]*)"', confirm_page)
        fields[name] = html.unescape(match.group(1)) if match else ""
    effect = re.search(r'name="effect"[^>]*>([^<]*)</textarea>', confirm_page)
    fields["effect"] = html.unescape(effect.group(1)) if effect else ""
    confirmed = browser.request(
        "POST", f"{path}/confirm", data={**fields, "csrf_token": token(confirm_page)}
    )
    check(confirmed.status_code == 303, f"confirm the claim ({fields['supplement']})")
    result = wait_for(browser, path, "What the research says", sleep)
    verdict = re.search(r'class="verdict__word">([^<]+)<', result)
    check(verdict is not None, f"result: {verdict.group(1) if verdict else 'none'}")
    check(
        'class="quote ' in result or "Not found" in result or "Insufficient" in result,
        "verified quotes or an honest no-result",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scc_web.smoke")
    parser.add_argument(
        "--claim",
        action="store_true",
        help="also run one canned claim (real LLM calls)",
    )
    args = parser.parse_args(argv)
    browser = Browser(os.environ.get("SCC_SMOKE_URL", "http://web:8000"))
    try:
        run(
            browser,
            os.environ["SCC_SMOKE_EMAIL"],
            read_secret(os.environ["SCC_SMOKE_PASSWORD_FILE"]),
            os.environ.get("SCC_EXPECT_VERSION") or None,
            args.claim,
        )
    except (SmokeFailed, httpx.HTTPError, KeyError, ValueError) as exc:
        print(f"FAIL  {exc}", flush=True)
        return 1
    print("smoke test passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
