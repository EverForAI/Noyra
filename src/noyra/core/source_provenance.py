"""Conservative source classification for persisted event references.

These labels describe direct source-event classes only. They are not trust,
authorization, safety, or instruction-precedence credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

MAX_SOURCE_EVENT_IDS = 64
MAX_SOURCE_EVENT_IDS_JSON_CHARS = 32_768
MAX_SOURCE_EVENT_ID_CHARS = 512


class SourceCategory(StrEnum):
    HUMAN_INTERACTION = "human_interaction"
    HUMAN_ASSISTANCE_REPLY = "human_assistance_reply"
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
    event_ids = parse_source_event_ids(source_event_ids_json)
    if event_ids is None:
        return _unknown_provenance(input_valid=False)
    if not isinstance(subject_id, str) or not subject_id or not subject_id.isascii():
        return _unknown_provenance(input_valid=True)
    if not event_ids:
        return _unknown_provenance(input_valid=True)

    placeholders = ",".join("?" for _ in event_ids)
    rows = connection.execute(
        "SELECT event_id,event_type,source,payload_archive_key FROM events "
        f"WHERE subject_id=? AND event_id IN ({placeholders})",
        (subject_id, *event_ids),
    ).fetchall()
    by_id = {str(row["event_id"]): row for row in rows}
    private_tables = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('private_conversation_messages','private_conversations')"
        ).fetchall()
    }
    withdrawn_ids: set[str] = set()
    if private_tables == {"private_conversation_messages", "private_conversations"}:
        withdrawn_ids = {
            str(row["event_id"])
            for row in connection.execute(
                "SELECT e.event_id FROM events e "
                "JOIN private_conversation_messages m "
                "ON CASE WHEN json_valid(e.payload_json) "
                "THEN json_extract(e.payload_json,'$.interaction_id') ELSE NULL END "
                "=m.interaction_id "
                "JOIN private_conversations c ON c.conversation_id=m.conversation_id "
                f"WHERE e.subject_id=? AND e.event_id IN ({placeholders}) "
                "AND c.status IN ('deleting','deleted')",
                (subject_id, *event_ids),
            ).fetchall()
        }
    categories: set[SourceCategory] = set()
    unclassified_count = 0
    withdrawn_count = 0
    for event_id in event_ids:
        row = by_id.get(event_id)
        if row is None:
            unclassified_count += 1
            continue
        if event_id in withdrawn_ids:
            withdrawn_count += 1
            continue
        # Archiving replaces the original payload with {}. The event kind alone
        # cannot prove whether its original private conversation was withdrawn.
        if row["payload_archive_key"] is not None:
            unclassified_count += 1
            continue
        category = classify_source_event_row(
            str(row["event_type"]), str(row["source"]), payload_archived=False
        )
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


def parse_source_event_ids(value: object) -> tuple[str, ...] | None:
    """Parse bounded direct references; invalid historical data has unknown lineage."""
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


def classify_source_event_row(
    event_type: str,
    source: str,
    *,
    payload_archived: bool = False,
) -> SourceCategory | None:
    """Classify exact event metadata without promoting archived rows or content."""
    if payload_archived:
        return None
    if event_type in {"interaction_received", "human_message"} and source == "human":
        return SourceCategory.HUMAN_INTERACTION
    if event_type == "assistance_reply" and source == "participant":
        return SourceCategory.HUMAN_ASSISTANCE_REPLY
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
