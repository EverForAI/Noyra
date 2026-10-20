"""Deterministic wire adaptation; local models and authorization inputs stay intact."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def uses_completion_token_limit(model: str) -> bool:
    # A verified reasoning option on a third-party alias does not establish
    # its token-parameter contract. Preserve that endpoint's legacy encoding.
    normalized = model.strip().casefold()
    return normalized.startswith(("o1", "o3", "o4", "gpt-5", "codex"))


def strict_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy and normalize schema nodes, never instance values or local defaults.

    Required nullable properties keep their original union with null. References
    stay local to the copied root, including recursive model definitions.
    """
    result = deepcopy(schema)

    def visit(node: dict[str, Any]) -> None:
        node.pop("default", None)
        if node.get("type") == "object" or "properties" in node:
            properties = node.setdefault("properties", {})
            node["required"] = list(properties)
            # Never silently narrow an explicitly declared dictionary schema.
            node.setdefault("additionalProperties", False)
        for keyword in ("properties", "$defs", "definitions", "patternProperties"):
            children = node.get(keyword)
            if isinstance(children, dict):
                for child in children.values():
                    if isinstance(child, dict):
                        visit(child)
        for keyword in ("items", "additionalProperties", "not", "if", "then", "else"):
            child = node.get(keyword)
            if isinstance(child, dict):
                visit(child)
        for keyword in ("anyOf", "allOf", "oneOf", "prefixItems"):
            children = node.get(keyword)
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        visit(child)

    visit(result)
    return result
