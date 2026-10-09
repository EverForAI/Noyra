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
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='private_conversation_messages'"
        ).fetchone()
        is None
    ):
        return ids
    withdrawn = connection.execute(
        f"SELECT 1 FROM events e JOIN private_conversation_messages m "
        "ON json_extract(e.payload_json,'$.interaction_id')=m.interaction_id "
        "JOIN private_conversations c ON c.conversation_id=m.conversation_id "
        f"WHERE e.subject_id=? AND e.event_id IN ({placeholders}) "
        "AND c.status IN ('deleting','deleted') LIMIT 1",
        (subject_id, *ids),
    ).fetchone()
    if withdrawn is not None:
        raise CausalValidationError("private source events have been withdrawn")
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
        receipts_installed = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='private_erasure_receipts'"
            ).fetchone()
            is not None
        )
        if (
            receipts_installed
            and connection.execute(
                "SELECT 1 FROM private_erasure_receipts WHERE record_id=? LIMIT 1", (source_id,)
            ).fetchone()
            is not None
        ):
            raise CausalValidationError("private causal source has been withdrawn")
        event = connection.execute(
            "SELECT 1 FROM events WHERE subject_id=? AND event_id=?", (subject_id, source_id)
        ).fetchone()
        if event is not None:
            validate_event_ids(connection, subject_id, (source_id,))
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
