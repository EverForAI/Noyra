"""Conservative source classification for persisted event references.

These labels describe direct source-event classes only. They are not trust,
authorization, safety, or instruction-precedence credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from .private_sources import assert_event_sources_available

MAX_SOURCE_EVENT_IDS = 64
MAX_SOURCE_EVENT_IDS_JSON_CHARS = 32_768
MAX_SOURCE_EVENT_ID_CHARS = 512


class SourceCategory(StrEnum):
    HUMAN_INTERACTION = "human_interaction"
    EXTERNAL_OBSERVATION = "external_observation"
    UNKNOWN = "unknown"


class SourceProvenanceStatus(StrEnum):
    DIRECT_REFERENCES_CLASSIFIED = "direct_references_classified"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Classification of one record's direct event references, never full ancestry."""

    status: SourceProvenanceStatus
    categories: tuple[SourceCategory, ...]
    source_count: int
    unclassified_count: int
    withdrawn_count: int
    input_valid: bool
    lineage_scope: Literal["direct_event_references_only"] = "direct_event_references_only"


def classify_source_event_ids_json(
    connection: Any,
    subject_id: str,
    source_event_ids_json: object,
) -> SourceProvenance:
    """Describe recognized source events; missing or legacy-unknown evidence stays unknown.

    This function intentionally does not inspect event payloads or follow causal parents.
    An exact event category is descriptive only and never grants instruction authority.
    """
    event_ids = _parse_event_ids(source_event_ids_json)
    if event_ids is None:
        return _unknown_provenance(input_valid=False)
    if not isinstance(subject_id, str) or not subject_id or not subject_id.isascii():
        return _unknown_provenance(input_valid=True)
    if not event_ids:
        return _unknown_provenance(input_valid=True)

    placeholders = ",".join("?" for _ in event_ids)
    rows = connection.execute(
        "SELECT event_id,event_type,source FROM events "
        f"WHERE subject_id=? AND event_id IN ({placeholders})",
        (subject_id, *event_ids),
    ).fetchall()
    by_id = {str(row["event_id"]): row for row in rows}
    categories: set[SourceCategory] = set()
    unclassified_count = 0
    withdrawn_count = 0
    for event_id in event_ids:
        row = by_id.get(event_id)
        if row is None:
            unclassified_count += 1
            continue
        try:
            assert_event_sources_available(connection, subject_id, event_id)
        except PermissionError:
            withdrawn_count += 1
            continue
        category = _classify_event(str(row["event_type"]), str(row["source"]))
        if category is None:
            unclassified_count += 1
        else:
            categories.add(category)

    if unclassified_count or withdrawn_count:
        categories.add(SourceCategory.UNKNOWN)
    if not categories:
        categories.add(SourceCategory.UNKNOWN)

    if withdrawn_count == len(event_ids) and unclassified_count == 0:
        status = SourceProvenanceStatus.WITHDRAWN
    elif unclassified_count == 0 and withdrawn_count == 0:
        status = SourceProvenanceStatus.DIRECT_REFERENCES_CLASSIFIED
    elif categories - {SourceCategory.UNKNOWN}:
        status = SourceProvenanceStatus.PARTIAL
    else:
        status = SourceProvenanceStatus.UNKNOWN
    return SourceProvenance(
        status=status,
        categories=tuple(sorted(categories, key=lambda item: item.value)),
        source_count=len(event_ids),
        unclassified_count=unclassified_count,
        withdrawn_count=withdrawn_count,
        input_valid=True,
    )


def _parse_event_ids(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, str) or len(value) > MAX_SOURCE_EVENT_IDS_JSON_CHARS:
        return None
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        return None
    if not isinstance(decoded, list) or len(decoded) > MAX_SOURCE_EVENT_IDS:
        return None
    if any(
        not isinstance(event_id, str)
        or not event_id
        or not event_id.isascii()
        or len(event_id) > MAX_SOURCE_EVENT_ID_CHARS
        or any(ord(character) < 32 for character in event_id)
        for event_id in decoded
    ):
        return None
    if len(set(decoded)) != len(decoded):
        return None
    return tuple(decoded)


def _classify_event(event_type: str, source: str) -> SourceCategory | None:
    if event_type in {"interaction_received", "human_message"} and source == "human":
        return SourceCategory.HUMAN_INTERACTION
    if event_type == "world_observation" and source == "safe_web_reader":
        return SourceCategory.EXTERNAL_OBSERVATION
    return None


def _unknown_provenance(*, input_valid: bool) -> SourceProvenance:
    return SourceProvenance(
        status=SourceProvenanceStatus.UNKNOWN,
        categories=(SourceCategory.UNKNOWN,),
        source_count=0,
        unclassified_count=0,
        withdrawn_count=0,
        input_valid=input_valid,
    )
