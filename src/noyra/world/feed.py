"""Bounded RSS/Atom extraction; identity and content revisions are independent."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from noyra.core.types import canonical_json, content_hash

from .errors import FetchError

# Keep the on-disk fingerprint contract at version 1. The semantic parser fix
# may produce one corrected version for an already-seen item; the old row stays
# immutable, the corrected row is bounded by the normal seen-cache, and later
# polls remain unchanged without a broad replay or schema migration.
FEED_FINGERPRINT_VERSION = 1
RDF_NAMESPACE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RSS1_NAMESPACE = "http://purl.org/rss/1.0/"
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
CONTENT_NAMESPACE = "http://purl.org/rss/1.0/modules/content/"
MAX_FEED_ITEMS = 512
MAX_FEED_BYTES = 2_000_000
MAX_FIELD_CHARS = 100_000


@dataclass(frozen=True)
class FeedItem:
    item_key: str
    fingerprint: str
    title: str
    link: str
    summary: str
    body: str

    def payload(self) -> dict[str, str]:
        return {"title": self.title, "link": self.link, "summary": self.summary, "body": self.body}


class _BoundedTree(ET.TreeBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.nodes = 0

    def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
        self.depth += 1
        self.nodes += 1
        if self.depth > 32 or self.nodes > 20_000:
            raise FetchError("world_feed_structure_limit")
        return super().start(tag, attrs)

    def end(self, tag: str) -> ET.Element:
        result = super().end(tag)
        self.depth -= 1
        return result

    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise FetchError("world_feed_dtd_rejected")


def normalize(value: str) -> str:
    if len(value) > MAX_FIELD_CHARS:
        # Reject rather than silently alias two revisions differing beyond a cutoff.
        raise FetchError("world_feed_field_limit")
    return " ".join(unicodedata.normalize("NFC", value).split())


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipped = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "template", "noscript"}:
            self.skipped += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "template", "noscript"} and self.skipped:
            self.skipped -= 1

    def handle_data(self, data: str) -> None:
        if not self.skipped:
            self.parts.append(data)


_HIDDEN_XML_ELEMENTS = frozenset({"script", "style", "template", "noscript"})


def _raw_text(element: ET.Element) -> str:
    value = " ".join(element.itertext())
    return normalize(value)


def _html_text(element: ET.Element) -> str:
    value = " ".join(element.itertext())
    if len(value) > MAX_FIELD_CHARS:
        raise FetchError("world_feed_field_limit")
    parser = _VisibleText()
    parser.feed(value)
    parser.close()
    return normalize(" ".join(parser.parts))


def _xhtml_text(element: ET.Element) -> str:
    parts: list[str] = []

    def visit(node: ET.Element) -> None:
        if _local(node.tag) in _HIDDEN_XML_ELEMENTS:
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            visit(child)
            if child.tail:
                parts.append(child.tail)

    visit(element)
    value = " ".join(parts)
    if len(value) > MAX_FIELD_CHARS:
        raise FetchError("world_feed_field_limit")
    return normalize(value)


def _field_text(element: ET.Element | None, *, atom: bool, html: bool = False) -> str:
    if element is None:
        return ""
    if not atom:
        return _html_text(element) if html else _raw_text(element)
    content_type = element.attrib.get("type", "text").strip().lower()
    if content_type in {"xhtml", "application/xhtml+xml"}:
        return _xhtml_text(element)
    if content_type in {"html", "text/html"}:
        return _html_text(element)
    # Atom text constructs default to plain text. XML entity decoding has
    # already happened once, so angle brackets remain literal data here.
    return _raw_text(element)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _link(raw: str, source_url: str) -> str:
    value = normalize(raw)
    if not value or len(value) > 2_048:
        return ""
    try:
        parsed = urlsplit(urljoin(source_url, value))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username or parsed.password:
            return ""
        return urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
        )
    except ValueError:
        return ""


def parse_feed(text: str, source_url: str) -> tuple[str | None, tuple[FeedItem, ...]]:
    if len(text.encode("utf-8")) > MAX_FEED_BYTES:
        raise FetchError("world_feed_size_limit")
    if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        raise FetchError("world_feed_dtd_rejected")
    try:
        root = ET.fromstring(text, parser=ET.XMLParser(target=_BoundedTree()))
    except (ET.ParseError, ValueError) as error:
        raise FetchError("world_feed_xml_invalid") from error
    if root.tag == "rss":
        prefix = ""
        channels = [child for child in root if child.tag == "channel"]
        if len(channels) != 1:
            raise FetchError("world_feed_channel_invalid")
        container = channels[0]
        entries = [child for child in container if child.tag == "item"]
    elif root.tag == f"{{{RDF_NAMESPACE}}}RDF":
        prefix = f"{{{RSS1_NAMESPACE}}}"
        # RDF/XML is a general data format, not itself a feed declaration.
        # A bounded non-RSS document may use the reader's existing generic XML
        # path; malformed RSS1 resources must never take that fallback.
        if not any(child.tag.startswith(prefix) for child in root):
            raise FetchError("world_feed_format_unsupported")
        channels = [child for child in root if child.tag == f"{prefix}channel"]
        if len(channels) != 1:
            raise FetchError("world_feed_channel_invalid")
        container = channels[0]
        entries = [child for child in root if child.tag == f"{prefix}item"]
    elif root.tag == f"{{{ATOM_NAMESPACE}}}feed":
        prefix = f"{{{ATOM_NAMESPACE}}}"
        container = root
        entries = [child for child in root if child.tag == f"{prefix}entry"]
    else:
        raise FetchError("world_feed_format_unsupported")
    if len(entries) > MAX_FEED_ITEMS:
        raise FetchError("world_feed_item_limit")
    is_atom = prefix == f"{{{ATOM_NAMESPACE}}}"
    title = (
        next(
            (_field_text(c, atom=is_atom) for c in container if c.tag == f"{prefix}title"),
            "",
        )
        or None
    )
    items: dict[tuple[str, str], FeedItem] = {}
    for entry in entries:
        # XML prefixes are aliases, not identities. Only the format's core
        # namespace (and the explicit RSS content extension) may supply fields.
        # Unknown extensions cannot shadow content, links, titles or IDs.
        fields = {child.tag: child for child in entry}
        item_title = _field_text(fields.get(f"{prefix}title"), atom=is_atom)
        if is_atom:
            summary = _field_text(fields.get(f"{prefix}summary"), atom=True)
            body = _field_text(fields.get(f"{prefix}content"), atom=True)
        else:
            summary = _field_text(fields.get(f"{prefix}description"), atom=False, html=True)
            if not summary:
                summary = _field_text(fields.get(f"{prefix}summary"), atom=False, html=True)
            body = _field_text(fields.get(f"{{{CONTENT_NAMESPACE}}}encoded"), atom=False, html=True)
            if not body:
                body = _field_text(fields.get(f"{prefix}content"), atom=False, html=True)
        links = [c for c in entry if c.tag == f"{prefix}link"]
        raw_link = next(
            (
                c.attrib.get("href", "")
                for c in links
                if c.attrib.get("rel", "alternate") == "alternate" and c.attrib.get("href")
            ),
            "",
        )
        if not raw_link and links:
            raw_link = "".join(links[0].itertext())
        link = _link(raw_link, source_url)
        identifier = None if is_atom else fields.get(f"{prefix}guid")
        if identifier is None:
            identifier = fields.get(f"{prefix}id")
        guid = normalize("".join(identifier.itertext())) if identifier is not None else ""
        if not guid and prefix == f"{{{RSS1_NAMESPACE}}}":
            guid = normalize(entry.attrib.get(f"{{{RDF_NAMESPACE}}}about", ""))
        payload = {"title": item_title, "link": link, "summary": summary, "body": body}
        if not any(payload.values()):
            continue
        identity = {"id": guid, "link": link} if guid or link else {"anonymous": payload}
        key = content_hash({"version": FEED_FINGERPRINT_VERSION, "identity": identity})
        fingerprint = content_hash({"version": FEED_FINGERPRINT_VERSION, "content": payload})
        items[(key, fingerprint)] = FeedItem(key, fingerprint, item_title, link, summary, body)
    return title, tuple(items[key] for key in sorted(items))


def feed_content(items: tuple[FeedItem, ...]) -> str:
    return canonical_json(
        {
            "feed_fingerprint_version": FEED_FINGERPRINT_VERSION,
            "items": [item.payload() for item in items],
        }
    )
