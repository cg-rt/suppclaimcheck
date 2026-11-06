"""The readable text of an HTML page (P3.12, D-055): what a reader sees, without menus, scripts or hidden parts.

Standard library only (`html.parser`), and deliberately simple:
- Skipped: scripts, styles, templates, forms, navigation, page headers and footers, asides, embedded
  frames, ARIA landmarks of the same kinds, and anything marked hidden (the `hidden` attribute,
  `aria-hidden="true"`, or an inline style with `display:none` or `visibility:hidden`). Hidden text is
  where instructions meant for AI tools usually sit, and a reader never sees it.
- Kept: one paragraph per block element, from the largest `<article>`, else `<main>`, else the whole body,
  whichever first holds at least `min_chars`.
- Also read: the title (Open Graph first), the site name, and whether the page says `noai` in a robots
  meta tag addressed to everyone or to our token.
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser

SKIP = frozenset(
    {
        "script", "style", "noscript", "template", "svg", "math", "iframe", "object", "embed",
        "canvas", "form", "button", "select", "textarea", "nav", "header", "footer", "aside", "dialog",
    }
)  # fmt: skip
SKIP_ROLES = frozenset(
    {
        "navigation", "banner", "contentinfo", "complementary", "search", "dialog", "alertdialog",
        "menu", "menubar",
    }
)  # fmt: skip
BLOCK = frozenset(
    {
        "address", "article", "blockquote", "body", "caption", "dd", "details", "div", "dl", "dt",
        "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "li", "main", "ol", "p", "pre",
        "section", "summary", "table", "td", "th", "tr", "ul",
    }
)  # fmt: skip
VOID = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
        "track", "wbr",
    }
)  # fmt: skip
# Elements whose end tag HTML lets authors leave out: a skipped one ends at the next block boundary.
OPTIONAL_END = frozenset({"p", "li", "dt", "dd", "tr", "td", "th", "caption"})
_SPACE = re.compile(r"\s+")
_MAX_META = 300


@dataclass
class Extracted:
    title: str | None
    site_name: str | None
    paragraphs: list[str]
    # The page asks not to be used by AI tools (`noai` in a robots meta tag for everyone or for us).
    noai: bool


def _squash(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def _hidden(attrs: dict[str, str]) -> bool:
    if "hidden" in attrs or attrs.get("aria-hidden", "").strip().lower() == "true":
        return True
    if attrs.get("role", "").strip().lower() in SKIP_ROLES:
        return True
    style = _SPACE.sub("", attrs.get("style", "").lower())
    return "display:none" in style or "visibility:hidden" in style


@dataclass
class _Segment:
    text: str
    article: int | None
    in_main: bool


class _Parser(HTMLParser):
    def __init__(self, token: str):
        super().__init__(convert_charrefs=True)
        self.token = token
        self.segments: list[_Segment] = []
        self.buffer: list[str] = []
        # The element being skipped, and how deep inside nested copies of it the parser is.
        self.skip_tag: str | None = None
        self.skip_depth = 0
        self.articles: list[int] = []
        self.article_count = 0
        self.main_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.first_h1: list[str] | None = None
        self.h1_open = False
        self.og_title: str | None = None
        self.site_name: str | None = None
        self.noai = False

    def flush(self) -> None:
        text = _squash("".join(self.buffer))
        self.buffer.clear()
        if text:
            article = self.articles[-1] if self.articles else None
            self.segments.append(_Segment(text, article, self.main_depth > 0))

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        if self.skip_tag is not None:
            # A <p> or <li> can't hold another block, so the next block ends it even without an end tag.
            if self.skip_tag in OPTIONAL_END and tag in BLOCK:
                self._end_skip()
                self.handle_starttag(tag, attrs)
            elif tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag == "title":
            self.in_title = True
            return
        if tag == "meta":
            self._meta(values)
            return
        if tag in VOID:
            if tag in {"br", "hr"}:
                self.flush()
            return
        if tag in SKIP or _hidden(values):
            if tag in BLOCK or tag in SKIP:
                self.flush()
            self.skip_tag, self.skip_depth = tag, 1
            return
        if tag in BLOCK:
            self.flush()
        if tag == "article":
            self.article_count += 1
            self.articles.append(self.article_count)
        elif tag == "main":
            self.main_depth += 1
        elif tag == "h1" and self.first_h1 is None:
            self.first_h1, self.h1_open = [], True

    def handle_startendtag(self, tag: str, attrs) -> None:
        # <br/>, <meta ... />: nothing to close.
        if self.skip_tag is None:
            if tag == "meta":
                self._meta({k.lower(): (v or "") for k, v in attrs})
            elif tag in {"br", "hr"}:
                self.flush()

    def handle_endtag(self, tag: str) -> None:
        if self.skip_tag is not None:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self._end_skip()
            elif self.skip_tag in OPTIONAL_END and tag in BLOCK:
                self._end_skip()
                self.handle_endtag(tag)
            return
        if tag == "title":
            self.in_title = False
            return
        if tag in BLOCK:
            self.flush()
        if tag == "article" and self.articles:
            self.articles.pop()
        elif tag == "main" and self.main_depth:
            self.main_depth -= 1
        elif tag == "h1":
            self.h1_open = False

    def handle_data(self, data: str) -> None:
        if self.skip_tag is not None:
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        self.buffer.append(data)
        if self.h1_open and self.first_h1 is not None:
            self.first_h1.append(data)

    def _end_skip(self) -> None:
        self.skip_tag, self.skip_depth = None, 0

    def _meta(self, attrs: dict[str, str]) -> None:
        name = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        content = _squash(attrs.get("content", ""))
        if name == "og:title" and content:
            self.og_title = content[:_MAX_META]
        elif name == "og:site_name" and content:
            self.site_name = content[:_MAX_META]
        elif name in {"robots", self.token}:
            directives = {
                d.strip() for d in content.lower().replace(" ", ",").split(",")
            }
            self.noai = self.noai or "noai" in directives


def _size(segments: list[str]) -> int:
    return sum(len(s) for s in segments)


def extract(html: str, token: str, min_chars: int = 200) -> Extracted:
    """The page's title, site name and readable paragraphs. `token` is our robots product token."""
    parser = _Parser(token=token.lower())
    parser.feed(html)
    parser.close()
    parser.flush()
    body = [s.text for s in parser.segments]
    by_article: dict[int, list[str]] = {}
    for s in parser.segments:
        if s.article is not None:
            by_article.setdefault(s.article, []).append(s.text)
    largest = max(by_article.values(), key=_size, default=[])
    main = [s.text for s in parser.segments if s.in_main]
    chosen = next((c for c in (largest, main) if _size(c) >= min_chars), body)
    title = (
        parser.og_title
        or _squash("".join(parser.title_parts))
        or _squash("".join(parser.first_h1 or []))
        or None
    )
    return Extracted(
        title=title[:_MAX_META] if title else None,
        site_name=parser.site_name,
        paragraphs=chosen,
        noai=parser.noai,
    )
