"""JATS XML to passages (P1.08): the abstract and body, section by section, paragraph by paragraph.

A passage is one paragraph, or several short paragraphs of the same section merged up to the size
limit. Tables, figures and back matter are left out. The text is what itertext() yields, with
whitespace collapsed: the same text the LLM sees and the quote check compares against.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_SPACE = re.compile(r"\s+")
_SKIP_TAGS = {"table-wrap", "fig", "disp-formula", "supplementary-material", "table"}


@dataclass(frozen=True)
class Passage:
    ordinal: int
    section: str
    text: str


def _text(element: ET.Element) -> str:
    parts: list[str] = []

    def walk(node: ET.Element) -> None:
        if node.tag in _SKIP_TAGS:
            if node.tail:
                parts.append(node.tail)
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
        if node.tail and node is not element:
            parts.append(node.tail)

    walk(element)
    return _SPACE.sub(" ", "".join(parts)).strip()


def _title(sec: ET.Element) -> str:
    title = sec.find("title")
    return _text(title) if title is not None else ""


def _paragraphs(container: ET.Element, label: str, skip: set[str]):
    """Yields (section label, paragraph text) in document order, nested sections included."""
    for child in container:
        if child.tag == "p":
            text = _text(child)
            if text:
                yield label, text
        elif child.tag == "sec":
            title = _title(child)
            if title.lower() in skip:
                continue
            yield from _paragraphs(
                child,
                f"{label}: {title}" if label and title else (title or label),
                skip,
            )


def passages(xml: str, max_chars: int, skip_sections: list[str]) -> list[Passage]:
    root = ET.fromstring(xml)
    skip = {s.lower() for s in skip_sections}
    pieces: list[tuple[str, str]] = []
    for abstract in root.iterfind("./front/article-meta/abstract"):
        if abstract.get("abstract-type") in {"graphical", "teaser"}:
            continue
        pieces.extend(_paragraphs(abstract, "Abstract", skip))
    body = root.find("./body")
    if body is not None:
        pieces.extend(_paragraphs(body, "", skip))

    merged: list[Passage] = []
    for section, text in pieces:
        section = section or "Body"
        last = merged[-1] if merged else None
        if (
            last
            and last.section == section
            and len(last.text) + 1 + len(text) <= max_chars
        ):
            merged[-1] = Passage(last.ordinal, section, f"{last.text} {text}")
        else:
            merged.append(Passage(len(merged), section, text))
    return merged
