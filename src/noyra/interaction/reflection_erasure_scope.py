"""Read-only evidence for reviewing sleep derivatives, never an erasure decision."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from noyra.core.source_provenance import MAX_SOURCE_EVENT_IDS

# A belief receipt can contain separate support and counter lists plus the operation ID.
_MAX_RECEIPT_IDS = 2 * MAX_SOURCE_EVENT_IDS + 1
_MAX_RECEIPT_BYTES = 128 * 1024


def sleep_reflection_scope(
    connection: sqlite3.Connection, subject: str, window: str, deadline: float, *, row_limit: int
) -> dict[str, Any]:
    """Report positive direct links in the caller's snapshot, not the transitive scope."""
    scope = {"coverage": "known_direct_links_only", "status": "bounded_out", "counts": None}
    if time.monotonic() >= deadline:
        return scope
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        counts = _counts(connection, subject, window, deadline, row_limit)
        if counts is not None and time.monotonic() < deadline:
            return {**scope, "status": "observed", "counts": counts}
        return scope
    except sqlite3.OperationalError as error:
        if error.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT and time.monotonic() >= deadline:
            return scope
        raise
    finally:
        connection.set_progress_handler(None, 0)


def _counts(
    connection: sqlite3.Connection, subject: str, window: str, deadline: float, row_limit: int
) -> dict[str, int] | None:
    # Rejected proposals and fallbacks can share a purpose with the committed
    # reflection. Only the exact binding written at commit supplies attribution.
    reflections = connection.execute(
        "SELECT r.sleep_id,EXISTS(SELECT 1 FROM private_model_sources s WHERE s.call_id=c.call_id "
        "AND s.subject_id=r.subject_id AND s.conversation_id=?) AS target_input,"
        "EXISTS(SELECT 1 FROM private_model_sources s WHERE s.call_id=c.call_id "
        "AND s.subject_id=r.subject_id AND s.conversation_id!=?) AS other_input "
        "FROM sleep_runs r JOIN sleep_reflections f ON f.sleep_id=r.sleep_id "
        "LEFT JOIN events e ON e.event_id=f.event_id AND e.subject_id=r.subject_id "
        "AND e.event_type='sleep_reflection' AND e.payload_archive_key IS NULL "
        "LEFT JOIN model_calls c ON c.subject_id=r.subject_id AND c.status='succeeded' "
        "AND c.purpose='sleep_reflection:'||r.sleep_id "
        "AND c.call_id=CASE WHEN json_valid(e.payload_json) "
        "THEN json_extract(e.payload_json,'$.model_call_id') END "
        "WHERE r.subject_id=? LIMIT ?",
        (window, window, subject, row_limit + 1),
    ).fetchall()
    if len(reflections) > row_limit:
        return None
    target_input_runs = {row["sleep_id"] for row in reflections if row["target_input"]}
    mixed_input_runs = {
        row["sleep_id"] for row in reflections if row["target_input"] and row["other_input"]
    }
    counts = {
        "committed_reflections": len(reflections),
        "reflections_without_recorded_model_inputs": sum(
            not (row["target_input"] or row["other_input"]) for row in reflections
        ),
        "reflections_with_target_model_input": len(target_input_runs),
        "reflections_with_multiple_window_inputs": len(mixed_input_runs),
        "memory_belief_receipts_with_target_model_input": 0,
        "memory_belief_receipts_with_multiple_window_inputs": 0,
        "memory_belief_receipts_with_target_event": 0,
        "target_receipts_with_unmatched_event_references": 0,
        "opaque_memory_belief_receipts": 0,
    }
    if not reflections:
        return counts
    events: set[str] = set()
    for row in connection.execute(
        "SELECT e.event_id FROM events e JOIN private_conversation_messages m "
        "ON m.subject_id=e.subject_id AND m.interaction_id=CASE WHEN json_valid(e.payload_json) "
        "THEN json_extract(e.payload_json,'$.interaction_id') END "
        "WHERE m.subject_id=? AND m.conversation_id=? AND e.payload_archive_key IS NULL LIMIT ?",
        (subject, window, row_limit + 1),
    ):
        events.add(row[0])
        if len(events) > row_limit or time.monotonic() >= deadline:
            return None
    receipts = connection.execute(
        "SELECT r.sleep_id,f.event_id,CASE WHEN length(CAST(i.source_ids_json AS BLOB))<=? "
        "THEN i.source_ids_json END AS sources "
        "FROM sleep_runs r JOIN sleep_reflections f ON f.sleep_id=r.sleep_id "
        "JOIN sleep_integrations i ON i.sleep_id=r.sleep_id "
        "WHERE r.subject_id=? AND i.integration_type IN ('memory','belief') LIMIT ?",
        (_MAX_RECEIPT_BYTES, subject, row_limit + 1),
    )
    for index, row in enumerate(receipts, start=1):
        if index > row_limit or time.monotonic() >= deadline:
            return None
        counts["memory_belief_receipts_with_target_model_input"] += (
            row["sleep_id"] in target_input_runs
        )
        counts["memory_belief_receipts_with_multiple_window_inputs"] += (
            row["sleep_id"] in mixed_input_runs
        )
        evidence = _receipt_evidence(row["sources"], row["event_id"])
        if evidence is None:
            counts["opaque_memory_belief_receipts"] += 1
        elif evidence & events:
            counts["memory_belief_receipts_with_target_event"] += 1
            # An additional reference is not proof of independent content.
            if evidence - events:
                counts["target_receipts_with_unmatched_event_references"] += 1
    return counts


def _receipt_evidence(encoded: object, operation_event: str) -> set[str] | None:
    if not isinstance(encoded, str):
        return None
    try:
        ids = json.loads(encoded)
    except (ValueError, RecursionError):
        return None
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= _MAX_RECEIPT_IDS
        or any(not isinstance(item, str) or not item for item in ids)
        or len(set(ids)) != len(ids)
    ):
        return None
    return (set(ids) - {operation_event}) or None
