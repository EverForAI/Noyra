"""Bound private thought projections without changing their durable sources."""

from __future__ import annotations

from typing import Any

from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash

from ._integrity import durable_boundary, durable_json, durable_string_list
from .types import SelfModelTraitProposal


def escape_context_data(serialized: str) -> str:
    data = serialized.replace("<", "\\u003c").replace(">", "\\u003e")
    return data.replace("BEGIN_", "BEGIN\\u005f").replace("END_", "END\\u005f")


def _encode(value: Any) -> str:
    return escape_context_data(canonical_json(value))


def _size(value: Any) -> int:
    return len(_encode(value).encode("utf-8"))


def _prefix(text: str, length: int) -> str:
    return text if len(text) <= length else text[:length] + "…"


def _agenda_projection(original: dict[str, Any], limit: int) -> dict[str, Any]:
    # Only the descriptive topic can shrink. IDs, lifecycle state, attention
    # values and timestamps continue to identify the actual selected agenda.
    result = dict(original)
    low, high = 0, len(original["topic"])
    while low < high:
        middle = (low + high + 1) // 2
        result["topic"] = _prefix(original["topic"], middle)
        if _size(result) <= limit:
            low = middle
        else:
            high = middle - 1
    result["topic"] = _prefix(original["topic"], low)
    if _size(result) > limit:
        raise IntegrityError("thought agenda identifiers exceed the context budget")
    return result


def _self_model_projection(original: dict[str, Any], limit: int) -> dict[str, Any]:
    columns = ("values_json", "traits_json", "commitments_json", "uncertainties_json")
    entries: dict[str, list[Any]] = {}
    for column in columns:
        if column == "traits_json":
            traits = durable_json(
                original[column], "thought self-model traits", original["version"]
            )
            with durable_boundary("thought self-model traits", original["version"]):
                if not isinstance(traits, list):
                    raise ValueError("traits must be an array")
                entries[column] = [
                    SelfModelTraitProposal.model_validate_json(canonical_json(item)).model_dump(
                        mode="json"
                    )
                    for item in traits
                ]
        else:
            entries[column] = list(
                durable_string_list(original[column], "thought self-model", original["version"])
            )
    width = limit
    while True:
        result = dict(original)
        result["continuity_statement"] = _prefix(original["continuity_statement"], width)
        for column in columns:
            # Never cut a serialized JSON string in the middle. Project its
            # text leaves and re-encode complete arrays/trait objects instead.
            values = [
                dict(item, trait=_prefix(item["trait"], width))
                if column == "traits_json"
                else _prefix(item, width)
                for item in entries[column]
            ]
            result[column] = canonical_json(values)
        if _size(result) <= limit:
            return result
        if width > 32:
            width = max(32, width // 2)
            continue
        removable = [column for column in columns if entries[column]]
        if not removable:
            raise IntegrityError("thought self-model identifiers exceed the context budget")
        largest = max(removable, key=lambda column: _size(result[column]))
        entries[largest].pop()


def fit_context(payload: dict[str, Any], limit: int) -> str:
    """Fit the final escaped UTF-8 representation, retaining an evidence anchor.

    Ordinary contexts keep their exact representation. If the dictionaries and
    one event cannot fit together, reserve a quarter for agenda metadata and a
    third for the self-model. Projection hashes commit to omitted text without
    copying it or making it citable evidence.
    """
    serialized = _encode(payload)
    if len(serialized.encode("utf-8")) <= limit:
        return serialized
    list_keys = (
        "events",
        "memories",
        "beliefs",
        "goals",
        "relationships",
        "affect",
        "recent_attempts",
        "common_knowledge_advisories",
    )
    core = {**payload, **{key: payload[key][:1] if key == "events" else [] for key in list_keys}}
    if _size(core) > limit:
        original_hashes = {}
        for key, budget, project in (
            ("agenda", limit // 4, _agenda_projection),
            ("self_model", limit // 3, _self_model_projection),
        ):
            original = payload[key]
            if original is not None and _size(original) > budget:
                original_hashes[key] = content_hash(original)
                payload[key] = project(original, budget)
        if original_hashes:
            payload["context_projection"] = {
                "format": "thought-context-projection/v1",
                "original_hashes": original_hashes,
            }
    while True:
        serialized = _encode(payload)
        if len(serialized.encode("utf-8")) <= limit:
            return serialized
        removable = [key for key in list_keys if len(payload[key]) > (1 if key == "events" else 0)]
        if not removable:
            raise IntegrityError("thought evidence identifiers exceed the context budget")
        largest = max(removable, key=lambda key: len(payload[key]))
        payload[largest].pop()
