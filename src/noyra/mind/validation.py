from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .errors import CausalValidationError


def validate_event_ids(
    connection: Any, subject_id: str, event_ids: Iterable[str]
) -> tuple[str, ...]:
    ids = tuple(dict.fromkeys(event_ids))
    if not ids:
        raise CausalValidationError("at least one source event is required")
    if len(ids) > 64:
        raise CausalValidationError("a state revision can reference at most 64 events")
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT event_id FROM events WHERE subject_id = ? AND event_id IN ({placeholders})",
        (subject_id, *ids),
    ).fetchall()
    found = {row["event_id"] for row in rows}
    missing = [event_id for event_id in ids if event_id not in found]
    if missing:
        raise CausalValidationError("source events are missing or belong to another subject")
    return ids


def validate_causal_source_ids(
    connection: Any, subject_id: str, source_ids: Iterable[str]
) -> tuple[str, ...]:
    ids = tuple(dict.fromkeys(source_ids))
    if not ids:
        raise CausalValidationError("at least one causal source is required")
    if len(ids) > 64:
        raise CausalValidationError("a state revision can reference at most 64 causal sources")
    entity_queries = (
        ("events", "event_id"),
        ("appraisals", "appraisal_id"),
        ("affect_transitions", "transition_id"),
        ("goals", "goal_id"),
        ("memories", "memory_id"),
        ("beliefs", "belief_id"),
        ("relationships", "relationship_id"),
        ("psychological_snapshots", "snapshot_id"),
    )
    for source_id in ids:
        if not any(
            connection.execute(
                f"SELECT 1 FROM {table} WHERE subject_id = ? AND {key} = ?",
                (subject_id, source_id),
            ).fetchone()
            is not None
            for table, key in entity_queries
        ):
            raise CausalValidationError("causal source is missing or belongs to another subject")
    return ids
