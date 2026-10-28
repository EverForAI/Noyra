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

FEED_FINGERPRINT_VERSION = 1
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


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    value = " ".join(element.itertext())
    if len(value) > MAX_FIELD_CHARS:
        raise FetchError("world_feed_field_limit")
    parser = _VisibleText()
    parser.feed(value)
    parser.close()
    return normalize(" ".join(parser.parts))


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
    kind = _local(root.tag)
    if kind == "rss":
        channels = [child for child in root if _local(child.tag) == "channel"]
        if len(channels) != 1:
            raise FetchError("world_feed_channel_invalid")
        container = channels[0]
        entries = [child for child in container if _local(child.tag) == "item"]
    elif root.tag == "{http://www.w3.org/2005/Atom}feed":
        container = root
        entries = [child for child in root if child.tag == "{http://www.w3.org/2005/Atom}entry"]
    else:
        raise FetchError("world_feed_format_unsupported")
    if len(entries) > MAX_FEED_ITEMS:
        raise FetchError("world_feed_item_limit")
    title = next((_text(c) for c in container if _local(c.tag) == "title"), "") or None
    items: dict[tuple[str, str], FeedItem] = {}
    for entry in entries:
        fields = {_local(child.tag): child for child in entry}
        item_title = _text(fields.get("title"))
        summary = _text(fields.get("description")) or _text(fields.get("summary"))
        body = _text(fields.get("encoded")) or _text(fields.get("content"))
        links = [c for c in entry if _local(c.tag) == "link"]
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
        identifier = fields.get("guid")
        if identifier is None:
            identifier = fields.get("id")
        guid = normalize("".join(identifier.itertext())) if identifier is not None else ""
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
