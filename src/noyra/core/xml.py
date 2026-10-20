"""Encoding-aware, DTD-free XML parsing with construction-time budgets."""

from __future__ import annotations

from xml.etree import ElementTree as ET


def parse_untrusted_xml(
    raw: bytes,
    *,
    max_nodes: int = 10_000,
    max_depth: int = 64,
    max_text_chars: int = 2_000_000,
) -> ET.Element:
    class BoundedTree(ET.TreeBuilder):
        def __init__(self) -> None:
            super().__init__()
            self.nodes = 0
            self.depth = 0
            self.text_size = 0

        def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
            raise ValueError("XML declarations are not allowed")

        def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
            self.nodes += 1
            self.depth += 1
            if self.nodes > max_nodes or self.depth > max_depth + 1:
                raise ValueError("XML structure exceeds limit")
            return super().start(tag, attrs)

        def end(self, tag: str) -> ET.Element:
            element = super().end(tag)
            self.depth -= 1
            return element

        def data(self, data: str) -> None:
            self.text_size += len(data)
            if self.text_size > max_text_chars:
                raise ValueError("XML text exceeds limit")
            super().data(data)

    return ET.fromstring(raw, parser=ET.XMLParser(target=BoundedTree()))
