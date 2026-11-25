"""Read-only, bounded evidence for reviewing sleep-derived private data."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from noyra.core.archive import ArchiveKeyring
from noyra.core.database import Database
from noyra.core.errors import (
    ArchiveAuthenticationError,
    ArchiveKeyUnavailableError,
    ArchiveUnavailableError,
    IntegrityError,
    PayloadLimitError,
)
from noyra.core.source_provenance import MAX_SOURCE_EVENT_IDS

from .public_cold_reader import (
    COLD_READ_SLOTS,
    PrivateArchiveReviewReader,
    archive_keyring_revision,
)

_MAX_RECEIPT_IDS = 2 * MAX_SOURCE_EVENT_IDS + 1
_MAX_RECEIPT_BYTES = 128 * 1024
_MAX_ARCHIVED_SEGMENTS = 128
_MAX_ARCHIVED_EVENTS = 20_000
_MAX_ARCHIVED_BYTES = 256_000_000
_MAX_ARCHIVE_SEGMENT_BYTES = 32_000_000
_PRIVATE_EVENT_TYPES = frozenset(
    {"interaction_received", "interaction_sent", "interaction_decision"}
)


def sleep_reflection_scope(
    connection: sqlite3.Connection,
    subject: str,
    window: str,
    deadline: float,
    *,
    row_limit: int,
    database: Database,
) -> dict[str, Any]:
    """Report direct evidence and its bounded descendants, never authorize deletion."""
    bounded: dict[str, Any] = {
        "coverage": "known_direct_links_and_reviewed_sleep_derivatives",
        "status": "bounded_out",
        "counts": None,
        "archive": {"status": "not_checked", "segments": 0, "events": 0},
    }
    if time.monotonic() >= deadline:
        return bounded
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        archive, archived_target_events, archived_model_calls = _archive_evidence(
            connection, database, subject, window, deadline
        )
        if archive["status"] not in {"not_present", "verified_local"}:
            return {**bounded, "archive": archive, "status": "incomplete"}
        counts = _counts(
            connection,
            subject,
            window,
            deadline,
            row_limit,
            archived_target_events,
            archived_model_calls,
        )
        if counts is None or time.monotonic() >= deadline:
            return {**bounded, "archive": archive}
        return {**bounded, "status": "observed", "counts": counts, "archive": archive}
    except IntegrityError:
        return {**bounded, "status": "incomplete"}
    except sqlite3.OperationalError as error:
        if error.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT and time.monotonic() >= deadline:
            return bounded
        raise
    finally:
        connection.set_progress_handler(None, 0)


def _archive_evidence(
    connection: sqlite3.Connection,
    database: Database,
    subject: str,
    window: str,
    deadline: float,
) -> tuple[dict[str, Any], set[str], dict[str, tuple[str, str | None]]]:
    result: dict[str, Any] = {
        "status": "unavailable",
        "segments": 0,
        "events": 0,
        "target_events": 0,
        "read_mode": "local_only_no_restore",
    }
    segments = connection.execute(
        "SELECT * FROM event_payload_segments WHERE subject_id=? "
        "ORDER BY created_at,segment_id LIMIT ?",
        (subject, _MAX_ARCHIVED_SEGMENTS + 1),
    ).fetchall()
    if not segments:
        dangling = connection.execute(
            "SELECT 1 FROM events e LEFT JOIN event_payload_segments s "
            "ON s.subject_id=e.subject_id AND s.object_key=e.payload_archive_key "
            "WHERE e.subject_id=? AND e.payload_archive_key IS NOT NULL AND s.segment_id IS NULL "
            "LIMIT 1",
            (subject,),
        ).fetchone()
        return (
            {
                **result,
                "status": "not_present" if dangling is None else "integrity_failed",
            },
            set(),
            {},
        )
    if len(segments) > _MAX_ARCHIVED_SEGMENTS:
        return {**result, "status": "bounded_out", "segments": len(segments)}, set(), {}
    if any(type(row["event_count"]) is not int or row["event_count"] < 1 for row in segments):
        return {**result, "status": "integrity_failed", "segments": len(segments)}, set(), {}
    declared_events = sum(row["event_count"] for row in segments)
    result.update(segments=len(segments), events=declared_events)
    if declared_events > _MAX_ARCHIVED_EVENTS:
        return {**result, "status": "bounded_out"}, set(), {}
    if time.monotonic() >= deadline:
        return result, set(), {}
    archived_rows = int(
        connection.execute(
            "SELECT COUNT(*) FROM events WHERE subject_id=? AND payload_archive_key IS NOT NULL",
            (subject,),
        ).fetchone()[0]
    )
    dangling = connection.execute(
        "SELECT 1 FROM events e LEFT JOIN event_payload_segments s "
        "ON s.subject_id=e.subject_id AND s.object_key=e.payload_archive_key "
        "WHERE e.subject_id=? AND e.payload_archive_key IS NOT NULL AND s.segment_id IS NULL "
        "LIMIT 1",
        (subject,),
    ).fetchone()
    if dangling is not None or archived_rows != declared_events:
        return {**result, "status": "integrity_failed"}, set(), {}

    messages = connection.execute(
        "SELECT interaction_id FROM private_conversation_messages "
        "WHERE subject_id=? AND conversation_id=? LIMIT ?",
        (subject, window, _MAX_ARCHIVED_EVENTS + 1),
    ).fetchall()
    if len(messages) > _MAX_ARCHIVED_EVENTS:
        return {**result, "status": "bounded_out"}, set(), {}
    target_interactions = {str(row["interaction_id"]) for row in messages}

    identities = [
        str(row[0])
        for row in connection.execute("SELECT subject_id FROM subject_identity LIMIT 10001")
    ]
    keys = [
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT subject_id,storage_key FROM subject_storage_keys LIMIT 10001"
        )
    ]
    if len(identities) > 10_000 or len(keys) > 10_000 or time.monotonic() >= deadline:
        return {**result, "status": "bounded_out"}, set(), {}
    if not COLD_READ_SLOTS.acquire(blocking=False):
        return {**result, "status": "reader_capacity"}, set(), {}

    reader: PrivateArchiveReviewReader | None = None
    target_events: set[str] = set()
    archived_model_calls: dict[str, tuple[str, str | None]] = {}
    total_bytes = 0
    try:
        keyring = ArchiveKeyring.from_env()
        keyring.validate_revision(database, subject, connection=connection)
        reader = PrivateArchiveReviewReader(
            database.path,
            subject,
            identities,
            keys,
            deadline,
            expected_keyring_revision=archive_keyring_revision(keyring),
        )
        reader.prepare()
        for segment in segments:
            if time.monotonic() >= deadline:
                return {**result, "status": "timed_out"}, set(), {}
            if (
                not isinstance(segment["object_key"], str)
                or type(segment["event_count"]) is not int
            ):
                return {**result, "status": "integrity_failed"}, set(), {}
            event_rows = connection.execute(
                "SELECT event_id,event_type,payload_hash FROM events "
                "WHERE subject_id=? AND payload_archive_key=? ORDER BY event_id LIMIT ?",
                (subject, segment["object_key"], 5_001),
            ).fetchall()
            if (
                len(event_rows) != segment["event_count"]
                or not event_rows
                or len(event_rows) > 5_000
                or time.monotonic() >= deadline
            ):
                return {**result, "status": "integrity_failed"}, set(), {}
            events = {
                row["event_id"]: (
                    row["payload_hash"],
                    row["event_type"]
                    if row["event_type"] in _PRIVATE_EVENT_TYPES | {"sleep_reflection"}
                    else "other",
                )
                for row in event_rows
            }
            maximum = min(_MAX_ARCHIVE_SEGMENT_BYTES, _MAX_ARCHIVED_BYTES - total_bytes)
            if maximum < 1:
                raise PayloadLimitError("private archive exceeds the review byte bound")
            references, bytes_read = reader.load_references(
                segment["object_key"], dict(segment), maximum, events
            )
            total_bytes += bytes_read
            for event_id, reference in references.items():
                if events[event_id][1] == "sleep_reflection":
                    archived_model_calls[event_id] = (
                        reference["sleep_id"],
                        reference.get("model_call_id"),
                    )
                elif reference["interaction_id"] in target_interactions:
                    target_events.add(event_id)
        if len(target_events) > _MAX_ARCHIVED_EVENTS:
            return {**result, "status": "bounded_out"}, set(), {}
        return (
            {
                **result,
                "status": "verified_local",
                "target_events": len(target_events),
            },
            target_events,
            archived_model_calls,
        )
    except PayloadLimitError:
        return {**result, "status": "bounded_out"}, set(), {}
    except TimeoutError:
        return {**result, "status": "timed_out"}, set(), {}
    except ArchiveAuthenticationError:
        return {**result, "status": "authentication_failed"}, set(), {}
    except ArchiveKeyUnavailableError:
        return {**result, "status": "key_unavailable"}, set(), {}
    except ArchiveUnavailableError:
        return {**result, "status": "unavailable"}, set(), {}
    except (IntegrityError, ValueError, OSError):
        return {**result, "status": "integrity_failed"}, set(), {}
    finally:
        if reader is not None:
            reader.close(COLD_READ_SLOTS.release)
        else:
            COLD_READ_SLOTS.release()


def _counts(
    connection: sqlite3.Connection,
    subject: str,
    window: str,
    deadline: float,
    row_limit: int,
    archived_target_events: set[str],
    archived_model_calls: dict[str, tuple[str, str | None]],
) -> dict[str, int] | None:
    reflections = connection.execute(
        "SELECT r.sleep_id,f.event_id,e.event_id AS matched_event_id,e.payload_archive_key,"
        "json_valid(e.payload_json) AS valid_payload,"
        "CASE WHEN e.payload_archive_key IS NULL AND json_valid(e.payload_json) "
        "THEN json_extract(e.payload_json,'$.model_call_id') END AS live_call_id,"
        "EXISTS(SELECT 1 FROM model_calls legacy WHERE legacy.subject_id=r.subject_id "
        "AND legacy.purpose='sleep_reflection:'||r.sleep_id AND legacy.status='succeeded') "
        "AS has_unbound_success "
        "FROM sleep_runs r JOIN sleep_reflections f ON f.sleep_id=r.sleep_id "
        "LEFT JOIN events e ON e.event_id=f.event_id AND e.subject_id=r.subject_id "
        "AND e.event_type='sleep_reflection' WHERE r.subject_id=? LIMIT ?",
        (subject, row_limit + 1),
    ).fetchall()
    if len(reflections) > row_limit:
        return None
    call_by_sleep: dict[str, str] = {}
    unbound_legacy_runs = 0
    for row in reflections:
        sleep_id = row["sleep_id"]
        if row["matched_event_id"] is None or not row["valid_payload"]:
            raise IntegrityError("sleep reflection event is missing or invalid")
        if row["payload_archive_key"] is not None:
            archived = archived_model_calls.get(row["event_id"])
            if archived is None or archived[0] != sleep_id:
                raise IntegrityError("archived sleep reflection link is inconsistent")
            call_id = archived[1]
        else:
            call_id = row["live_call_id"]
        if call_id is not None:
            if not isinstance(call_id, str) or not call_id:
                raise IntegrityError("sleep reflection model call reference is invalid")
            call_by_sleep[sleep_id] = call_id
        elif row["has_unbound_success"]:
            # Same-purpose responses cannot identify the selected proposal.
            unbound_legacy_runs += 1

    call_windows = {
        row["sleep_id"]: (bool(row["target_input"]), bool(row["other_input"]))
        for row in connection.execute(
            "SELECT refs.key AS sleep_id,"
            "MAX(CASE WHEN s.conversation_id=? THEN 1 ELSE 0 END) AS target_input,"
            "MAX(CASE WHEN s.conversation_id!=? THEN 1 ELSE 0 END) AS other_input "
            "FROM json_each(?) refs JOIN model_calls c ON c.call_id=refs.value "
            "AND c.subject_id=? AND c.status='succeeded' "
            "AND c.purpose='sleep_reflection:'||refs.key "
            "LEFT JOIN private_model_sources s ON s.call_id=c.call_id "
            "AND s.subject_id=c.subject_id "
            "GROUP BY refs.key",
            (window, window, json.dumps(call_by_sleep, separators=(",", ":")), subject),
        )
    }
    if call_by_sleep.keys() != call_windows.keys():
        raise IntegrityError("sleep reflection model call does not match its run")
    target_runs = {sleep_id for sleep_id, (target, _) in call_windows.items() if target}
    mixed_runs = {sleep_id for sleep_id, flags in call_windows.items() if all(flags)}
    counts = {
        "committed_reflections": len(reflections),
        "reflections_without_recorded_model_inputs": len(reflections)
        - sum(any(flags) for flags in call_windows.values()),
        "reflections_with_unbound_model_calls": unbound_legacy_runs,
        "reflections_with_target_model_input": len(target_runs),
        "reflections_with_multiple_window_inputs": len(mixed_runs),
        "memory_belief_receipts_with_target_model_input": 0,
        "memory_belief_receipts_with_multiple_window_inputs": 0,
        "memory_belief_receipts_with_target_event": 0,
        "personality_candidate_receipts_with_target_model_input": 0,
        "personality_candidate_receipts_with_multiple_window_inputs": 0,
        "personality_candidate_receipts_with_target_event": 0,
        "target_receipts_with_unmatched_event_references": 0,
        "opaque_memory_belief_receipts": 0,
        "opaque_derivative_receipts": 0,
        "sleep_integration_receipts": 0,
        "sleep_integration_receipts_with_target_model_input": 0,
        "sleep_integration_receipts_with_multiple_window_inputs": 0,
        "retry_blocks_linked_to_target_model_input": 0,
        "public_diary_entries_linked_to_target_model_input": 0,
        "project_sleep_reviews_on_target_model_run": 0,
    }
    target_events = set(archived_target_events)
    if len(target_events) > row_limit:
        return None
    for row in connection.execute(
        "SELECT e.event_id FROM events e JOIN private_conversation_messages m "
        "ON m.subject_id=e.subject_id AND m.interaction_id=CASE WHEN json_valid(e.payload_json) "
        "THEN json_extract(e.payload_json,'$.interaction_id') END "
        "WHERE m.subject_id=? AND m.conversation_id=? AND e.payload_archive_key IS NULL "
        "AND e.event_type IN ('interaction_received','interaction_sent','interaction_decision') "
        "LIMIT ?",
        (subject, window, row_limit + 1),
    ):
        target_events.add(str(row[0]))
        if len(target_events) > row_limit or time.monotonic() >= deadline:
            return None
    receipts = connection.execute(
        "SELECT r.sleep_id,f.event_id,i.integration_type,"
        "CASE WHEN length(CAST(i.source_ids_json AS BLOB))<=? "
        "THEN i.source_ids_json END AS sources FROM sleep_runs r "
        "JOIN sleep_reflections f ON f.sleep_id=r.sleep_id "
        "JOIN sleep_integrations i ON i.sleep_id=r.sleep_id "
        "WHERE r.subject_id=? LIMIT ?",
        (_MAX_RECEIPT_BYTES, subject, row_limit + 1),
    )
    for index, row in enumerate(receipts, start=1):
        if index > row_limit or time.monotonic() >= deadline:
            return None
        counts["sleep_integration_receipts"] += 1
        sleep_id = str(row["sleep_id"])
        counts["sleep_integration_receipts_with_target_model_input"] += sleep_id in target_runs
        counts["sleep_integration_receipts_with_multiple_window_inputs"] += sleep_id in mixed_runs
        integration_type = str(row["integration_type"])
        if integration_type in {"memory", "belief"}:
            counts["memory_belief_receipts_with_target_model_input"] += sleep_id in target_runs
            counts["memory_belief_receipts_with_multiple_window_inputs"] += sleep_id in mixed_runs
        elif integration_type == "personality_candidate":
            counts["personality_candidate_receipts_with_target_model_input"] += (
                sleep_id in target_runs
            )
            counts["personality_candidate_receipts_with_multiple_window_inputs"] += (
                sleep_id in mixed_runs
            )
        evidence = _receipt_evidence(row["sources"], row["event_id"])
        if evidence is None:
            counts["opaque_derivative_receipts"] += 1
            counts["opaque_memory_belief_receipts"] += integration_type in {"memory", "belief"}
        elif evidence & target_events:
            if integration_type in {"memory", "belief"}:
                counts["memory_belief_receipts_with_target_event"] += 1
            elif integration_type == "personality_candidate":
                counts["personality_candidate_receipts_with_target_event"] += 1
            if integration_type in {"memory", "belief"} and evidence - target_events:
                counts["target_receipts_with_unmatched_event_references"] += 1

    retry_rows = connection.execute(
        "SELECT source_sleep_id FROM retry_blocks WHERE subject_id=? LIMIT ?",
        (subject, row_limit + 1),
    ).fetchall()
    diary_rows = connection.execute(
        "SELECT d.source_sleep_id FROM public_diary_entries d JOIN sleep_runs s "
        "ON s.sleep_id=d.source_sleep_id AND d.subject_id=s.subject_id "
        "WHERE s.subject_id=? LIMIT ?",
        (subject, row_limit + 1),
    ).fetchall()
    project_rows = connection.execute(
        "SELECT p.sleep_id FROM autonomous_project_sleep_reflections p "
        "JOIN sleep_runs s ON s.sleep_id=p.sleep_id AND p.subject_id=s.subject_id "
        "WHERE s.subject_id=? LIMIT ?",
        (subject, row_limit + 1),
    ).fetchall()
    if any(len(rows) > row_limit for rows in (retry_rows, diary_rows, project_rows)):
        return None
    counts["retry_blocks_linked_to_target_model_input"] = sum(
        str(row["source_sleep_id"]) in target_runs for row in retry_rows
    )
    counts["public_diary_entries_linked_to_target_model_input"] = sum(
        str(row["source_sleep_id"]) in target_runs for row in diary_rows
    )
    counts["project_sleep_reviews_on_target_model_run"] = sum(
        str(row["sleep_id"]) in target_runs for row in project_rows
    )
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
