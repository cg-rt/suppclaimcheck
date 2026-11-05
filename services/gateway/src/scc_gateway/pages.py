"""Reading a web page a user gave (P3.12, D-055): the only code that fetches an address from outside.

The risk is server-side request forgery: an address that points back inside our own network. So:
- **Addresses:** http and https only, the ports in config (80 and 443), no user name or password, and a
  host name with a dot or a literal IP address.
- **Every connection** (the page, each redirect, robots.txt) resolves the host here and goes ahead only
  if every address it resolves to is public (`is_public`). Private, loopback, link-local (where cloud
  metadata lives), shared, multicast and reserved ranges are refused, IPv4 inside IPv6 included. The
  socket connects to the address that was checked, so a second DNS answer can't swap in another one
  (DNS rebinding). TLS still verifies the certificate for the host name.
- **Redirects** are followed by hand, at most `max_redirects`, each one checked again, robots.txt included.
- **Limits:** the whole read, robots.txt and redirects included, stops after `timeout_s`; bodies stop at
  `max_bytes` after decompression; only HTML is read. No cookies, no proxies, no JavaScript.

Manners: robots.txt (RFC 9309) is obeyed for `robots_token` and cached per site; one request per site
per `min_interval_s`; the user agent links to a page that says what the reader does. A page marked
`noai` (X-Robots-Tag or a robots meta tag) is not read.
"""

import asyncio
import codecs
import ipaddress
import logging
import re
import socket
import time
import zlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpcore

from scc_gateway import html_text

log = logging.getLogger("scc.pages")

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[Address]]]

MAX_URL = 2048
REDIRECTS = {301, 302, 303, 307, 308}
HTML_TYPES = {"text/html", "application/xhtml+xml"}
NAT64 = ipaddress.ip_network("64:ff9b::/96")
# Robots files of this many sites are kept in memory; the oldest goes first.
ROBOTS_CACHE_SITES = 512
# X-Robots-Tag directives that take a value after a colon, so the colon doesn't name a user agent.
_VALUED = {"unavailable_after", "max-snippet", "max-image-preview", "max-video-preview"}
_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_.:-]+)""", re.IGNORECASE
)
_HOST = re.compile(r"[a-z0-9.-]+")


class PageRefused(Exception):
    """The page was not read. `code` is for the caller; `message` is technical, not for users."""

    def __init__(self, code: str, message: str, host: str | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.host = code, message, host


class Blocked(Exception):
    """A connection was refused: the host resolves to an address that is not public."""


def is_public(address: Address) -> bool:
    """True only for globally routable unicast addresses. IPv4 carried inside IPv6 is judged as IPv4."""
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped or address.sixtofour
        if embedded is None and address in NAT64:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public(embedded)
        if address.teredo is not None:
            return False
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def system_resolve(host: str, port: int) -> list[Address]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    found: list[Address] = []
    for *_, sockaddr in infos:
        address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        if address not in found:
            found.append(address)
    return found


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """httpcore's network layer with the address check: every TCP connection goes through here."""

    def __init__(
        self,
        resolve: Resolver | None = None,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ):
        self.resolve = resolve or system_resolve
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        try:
            addresses = await self.resolve(host, port)
        except (OSError, UnicodeError) as exc:
            raise httpcore.ConnectError(f"{host} could not be resolved") from exc
        if not addresses:
            raise httpcore.ConnectError(f"{host} has no address")
        if not all(is_public(a) for a in addresses):
            raise Blocked(host)
        error: Exception | None = None
        for address in addresses:
            try:
                return await self.inner.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
        raise error  # type: ignore[misc]

    async def connect_unix_socket(self, *args, **kwargs):
        raise Blocked("unix socket")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


@dataclass(frozen=True)
class Target:
    """A checked address: what the request line, the Host header and the robots.txt lookup use."""

    url: str
    scheme: str
    host: str
    port: int
    # The path and query, as sent and as robots.txt rules match them.
    path: str

    @property
    def site(self) -> str:
        """scheme://host:port: robots.txt and its cache are per site."""
        return f"{self.scheme}://{_netloc(self.host, self.port, None)}"


def _netloc(host: str, port: int, scheme: str | None) -> str:
    shown = f"[{host}]" if ":" in host else host
    default = {"http": 80, "https": 443}.get(scheme or "")
    return shown if port == default else f"{shown}:{port}"


def target(url: str, ports: list[int]) -> Target:
    """Checks and normalises an address, or raises url_invalid or url_blocked. Doesn't resolve it."""
    url = url.strip()
    if len(url) > MAX_URL or any(c.isspace() or ord(c) < 32 for c in url):
        raise PageRefused("url_invalid", "not a single web address")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise PageRefused("url_invalid", "the address can't be parsed") from None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise PageRefused("url_invalid", "only http and https addresses are read")
    if parts.username is not None or parts.password is not None:
        raise PageRefused("url_invalid", "addresses with a user name are not read")
    name = (parts.hostname or "").rstrip(".")
    if not name:
        raise PageRefused("url_invalid", "the address has no host")
    try:
        literal = ipaddress.ip_address(name)
    except ValueError:
        literal = None
    if literal is not None:
        host = literal.compressed
        if not is_public(literal):
            raise PageRefused("url_blocked", "a private or reserved address", host)
    else:
        try:
            host = name.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise PageRefused("url_invalid", "the host name is not valid") from None
        if "." not in host or not _HOST.fullmatch(host):
            raise PageRefused("url_invalid", "the host name is not valid", host)
    port = port or (443 if scheme == "https" else 80)
    if port not in ports:
        raise PageRefused("url_blocked", f"port {port} is not read", host)
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="/?%:@!$&'()*+,;=-._~")
    return Target(
        url=urlunsplit((scheme, _netloc(host, port, scheme), path, query, "")),
        scheme=scheme,
        host=host,
        port=port,
        path=path + (f"?{query}" if query else ""),
    )


# robots.txt (RFC 9309)


@dataclass(frozen=True)
class Robots:
    # (allowed, pattern) for our group: ours if named, else "*"'s.
    rules: tuple[tuple[bool, str], ...] = ()

    def allows(self, path: str) -> bool:
        """The longest matching pattern decides; on a tie, allow wins. No match: allowed."""
        if path == "/robots.txt":
            return True
        best: tuple[int, bool] | None = None
        for allowed, pattern in self.rules:
            if _rule(pattern).match(path):
                key = (len(pattern), allowed)
                best = key if best is None or key > best else best
        return best is None or best[1]


ALLOW_ALL = Robots()


@lru_cache(maxsize=4096)
def _rule(pattern: str) -> re.Pattern:
    """A robots.txt path pattern: `*` matches anything, a final `$` anchors the end."""
    pattern = quote(pattern, safe="/%:@!$&'()*+,;=-._~?")
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""), re.DOTALL)


def parse_robots(text: str, token: str) -> Robots:
    """The rules that apply to `token`: every group naming it, else every `*` group (RFC 9309 2.2.1)."""
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    in_agents = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not in_agents:
                groups.append(([], []))
            groups[-1][0].append(value.lower())
            in_agents = True
        elif key in {"allow", "disallow"}:
            in_agents = False
            if groups and value:
                groups[-1][1].append((key == "allow", value))
    token = token.lower()

    def names_us(agent: str) -> bool:
        return agent.split("/", 1)[0].strip() == token

    mine = [rules for agents, rules in groups if any(names_us(a) for a in agents)]
    if not mine:
        mine = [rules for agents, rules in groups if "*" in agents]
    return Robots(tuple(rule for rules in mine for rule in rules))


def says_noai(values: list[str], token: str) -> bool:
    """X-Robots-Tag: `noai` for everyone, or after our token (`SuppClaimCheck: noai`)."""
    for value in values:
        agent = None
        for part in value.lower().split(","):
            part = part.strip()
            named = re.match(r"^([a-z0-9_.-]+)\s*:\s*(.*)$", part)
            if named and named.group(1) not in _VALUED:
                agent, part = named.group(1), named.group(2).strip()
            if part == "noai" and agent in {None, token}:
                return True
    return False


@dataclass(frozen=True)
class Page:
    url: str
    final_url: str
    title: str | None
    site_name: str | None
    text: str
    # The text was longer than `max_text_chars` and was cut at a paragraph.
    truncated: bool
    bytes: int
    host: str
    redirects: int


def _header(response: httpcore.Response, name: bytes) -> list[str]:
    return [v.decode("latin-1") for k, v in response.headers if k.lower() == name]


def _first(response: httpcore.Response, name: bytes) -> str:
    values = _header(response, name)
    return values[0].strip() if values else ""


def _charset(content_type: str, body: bytes) -> str:
    named = re.search(r"charset\s*=\s*\"?([A-Za-z0-9_.:-]+)", content_type)
    found = named.group(1) if named else None
    if found is None and (meta := _CHARSET.search(body[:4096])):
        found = meta.group(1).decode("ascii", "replace")
    try:
        return codecs.lookup(found or "utf-8").name
    except LookupError:
        return "utf-8"


def _looks_like_html(body: bytes) -> bool:
    start = body[:1024].lstrip().lower()
    return start.startswith((b"<!doctype html", b"<html")) or b"<html" in start


class PageReader:
    def __init__(
        self,
        config: dict,
        backend: httpcore.AsyncNetworkBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = config["pages"]
        self.pool = httpcore.AsyncConnectionPool(
            network_backend=backend or GuardedBackend(),
            max_connections=20,
            keepalive_expiry=5.0,
            retries=0,
        )
        self.clock = clock
        self.robots: OrderedDict[str, tuple[float, Robots]] = OrderedDict()
        self.turns: dict[str, float] = {}
        self.turn_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.pool.aclose()

    async def read(self, url: str) -> Page:
        start = target(url, self.cfg["ports"])
        try:
            async with asyncio.timeout(self.cfg["timeout_s"]):
                return await self._read(start)
        except TimeoutError:
            raise PageRefused(
                "page_timeout", f"no page within {self.cfg['timeout_s']} s", start.host
            ) from None
        except Blocked:
            raise PageRefused(
                "url_blocked", "resolves to a private or reserved address", start.host
            ) from None
        except (
            httpcore.NetworkError,
            httpcore.TimeoutException,
            httpcore.ProtocolError,
            httpcore.UnsupportedProtocol,
        ) as exc:
            raise PageRefused(
                "page_unavailable",
                f"connection failed: {type(exc).__name__}",
                start.host,
            ) from None

    def _headers(self, accept: str) -> list[tuple[bytes, bytes]]:
        return [
            (b"User-Agent", self.cfg["user_agent"].encode()),
            (b"Accept", accept.encode()),
            (b"Accept-Encoding", b"gzip"),
        ]

    async def _read(self, start: Target) -> Page:
        current = start
        for hop in range(self.cfg["max_redirects"] + 1):
            if self._blocked_site(current.host):
                raise PageRefused(
                    "robots_disallowed", "the site is on blocked_hosts", current.host
                )
            robots = await self._robots(current)
            if not robots.allows(current.path):
                raise PageRefused(
                    "robots_disallowed", "robots.txt disallows this path", current.host
                )
            await self._turn(current.host)
            async with self.pool.stream(
                "GET",
                current.url,
                headers=self._headers("text/html, application/xhtml+xml;q=0.9"),
            ) as response:
                if response.status in REDIRECTS:
                    location = _first(response, b"location")
                    if not location:
                        raise PageRefused(
                            "page_unavailable",
                            "a redirect without a location",
                            current.host,
                        )
                    current = target(urljoin(current.url, location), self.cfg["ports"])
                    continue
                if response.status in {404, 410}:
                    raise PageRefused(
                        "page_not_found",
                        f"the site answered {response.status}",
                        current.host,
                    )
                if response.status in {401, 403, 451}:
                    # Often a site that serves only browsers: the reader never pretends to be one.
                    raise PageRefused(
                        "site_refused",
                        f"the site answered {response.status}",
                        current.host,
                    )
                if not 200 <= response.status < 300 or response.status in {204, 206}:
                    raise PageRefused(
                        "page_unavailable",
                        f"the site answered {response.status}",
                        current.host,
                    )
                if says_noai(
                    _header(response, b"x-robots-tag"), self.cfg["robots_token"].lower()
                ):
                    raise PageRefused(
                        "ai_disallowed", "X-Robots-Tag says noai", current.host
                    )
                content_type = _first(response, b"content-type")
                media = content_type.split(";", 1)[0].strip().lower()
                if media and media not in HTML_TYPES:
                    raise PageRefused("not_html", f"content type {media}", current.host)
                declared = _first(response, b"content-length")
                if declared.isdigit() and int(declared) > self.cfg["max_bytes"]:
                    raise PageRefused(
                        "too_large", f"{declared} bytes declared", current.host
                    )
                body = await self._body(response, self.cfg["max_bytes"], current.host)
            if not media and not _looks_like_html(body):
                raise PageRefused("not_html", "no content type, not HTML", current.host)
            # Parsing up to 2 MB of HTML is CPU work: a thread keeps the gateway's other calls moving.
            return await asyncio.to_thread(
                self._page, start, current, body, content_type, hop
            )
        raise PageRefused("page_unavailable", "too many redirects", current.host)

    async def _body(
        self, response: httpcore.Response, limit: int, host: str, cut: bool = False
    ) -> bytes:
        """The body after decompression, refused (or cut, for robots.txt) past `limit` bytes."""
        encoding = _first(response, b"content-encoding").lower()
        if encoding in {"", "identity"}:
            decoder = None
        elif encoding in {"gzip", "x-gzip"}:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            decoder = zlib.decompressobj()
        else:
            raise PageRefused("page_unavailable", f"content encoding {encoding}", host)
        chunks: list[bytes] = []
        size = 0
        try:
            async for chunk in response.aiter_stream():
                if decoder is not None:
                    # max_length bounds what one compressed chunk may expand to (a zip bomb).
                    chunk = decoder.decompress(chunk, limit + 1 - size)
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    if cut:
                        return b"".join(chunks)[:limit]
                    raise PageRefused("too_large", f"more than {limit} bytes", host)
            if decoder is not None:
                chunks.append(decoder.flush(limit + 1 - size))
        except zlib.error:
            raise PageRefused(
                "page_unavailable", "the compressed body is damaged", host
            ) from None
        body = b"".join(chunks)
        if len(body) > limit:
            if cut:
                return body[:limit]
            raise PageRefused("too_large", f"more than {limit} bytes", host)
        return body

    def _page(
        self, start: Target, final: Target, body: bytes, content_type: str, hops: int
    ) -> Page:
        html = body.decode(_charset(content_type, body), errors="replace")
        extracted = html_text.extract(
            html, self.cfg["robots_token"], self.cfg["min_text_chars"]
        )
        if extracted.noai:
            raise PageRefused(
                "ai_disallowed", "a robots meta tag says noai", final.host
            )
        kept: list[str] = []
        size = 0
        limit = self.cfg["max_text_chars"]
        for paragraph in extracted.paragraphs:
            if size + len(paragraph) > limit:
                break
            kept.append(paragraph)
            size += len(paragraph) + 2
        text = "\n\n".join(kept)
        if len(text) < self.cfg["min_text_chars"]:
            raise PageRefused("no_text", f"{len(text)} characters of text", final.host)
        return Page(
            url=start.url,
            final_url=final.url,
            title=extracted.title,
            site_name=extracted.site_name,
            text=text,
            truncated=len(kept) < len(extracted.paragraphs),
            bytes=len(body),
            host=final.host,
            redirects=hops,
        )

    def _blocked_site(self, host: str) -> bool:
        """A site whose owner asked us to stay away (RB-17): the name or any name under it."""
        return any(
            host == blocked or host.endswith("." + blocked)
            for blocked in self.cfg["blocked_hosts"]
        )

    async def _turn(self, host: str) -> None:
        """At most one request to a site every `min_interval_s`, across all concurrent reads."""
        interval = self.cfg["min_interval_s"]
        async with self.turn_lock:
            now = self.clock()
            at = max(now, self.turns.get(host, now - interval) + interval)
            self.turns[host] = at
            if len(self.turns) > 4096:
                self.turns = {h: t for h, t in self.turns.items() if t > now - interval}
        if at > now:
            await asyncio.sleep(at - now)

    async def _robots(self, site: Target) -> Robots:
        cached = self.robots.get(site.site)
        if cached is not None and cached[0] > self.clock():
            self.robots.move_to_end(site.site)
            return cached[1]
        robots = await self._fetch_robots(site)
        self.robots[site.site] = (
            self.clock() + self.cfg["robots_cache_minutes"] * 60,
            robots,
        )
        while len(self.robots) > ROBOTS_CACHE_SITES:
            self.robots.popitem(last=False)
        return robots

    async def _fetch_robots(self, site: Target) -> Robots:
        """RFC 9309: a 4xx means no rules; 5xx or no answer means we can't know, so the page isn't read."""
        location = target(f"{site.site}/robots.txt", self.cfg["ports"])
        for _ in range(self.cfg["max_redirects"] + 1):
            await self._turn(location.host)
            async with self.pool.stream(
                "GET", location.url, headers=self._headers("text/plain")
            ) as response:
                if response.status in REDIRECTS:
                    moved = _first(response, b"location")
                    if not moved:
                        return ALLOW_ALL
                    location = target(urljoin(location.url, moved), self.cfg["ports"])
                    continue
                if 200 <= response.status < 300:
                    body = await self._body(
                        response, self.cfg["robots_max_bytes"], site.host, cut=True
                    )
                    return parse_robots(
                        body.decode("utf-8", errors="replace"), self.cfg["robots_token"]
                    )
                if 400 <= response.status < 500:
                    return ALLOW_ALL
                raise PageRefused(
                    "page_unavailable",
                    f"robots.txt answered {response.status}",
                    site.host,
                )
        return ALLOW_ALL
