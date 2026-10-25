"""Test-only, read-only R01 evidence survey. Not a product verifier or migrator.

Correlation never proves the original identity: RelationshipStore historically
accepted any same-subject source event, including events about another entity.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from noyra.core.events import EventStore
from noyra.core.types import content_hash, strict_json_loads


@dataclass(frozen=True)
class Evidence:
    relationship_id: str
    subject_id: str
    entity_type: str
    entity_key: str
    category: str
    reasons: tuple[str, ...]
    identity_at_creation_proven: bool = False
    matching_social_snapshots: int = 0


@contextmanager
def open_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("survey requires an existing regular database")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        c.execute("PRAGMA busy_timeout=200")
        c.execute("BEGIN")
        try:
            yield c
        finally:
            c.rollback()


def _tables(c: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _chain(c: sqlite3.Connection, subject: str, tables: set[str]) -> str | None:
    if "event_chain_roots" not in tables:
        return "event_chain_unavailable"
    count = c.execute("SELECT COUNT(*) FROM events WHERE subject_id=?", (subject,)).fetchone()[0]
    if count > 10000:
        raise ValueError("survey event budget exceeded; do not silently truncate")
    roots = c.execute(
        "SELECT COUNT(*) FROM event_chain_roots WHERE subject_id=?", (subject,)
    ).fetchone()[0]
    if count != roots:
        return "event_chain_invalid"
    previous = None
    verified = 0
    for sequence, row in enumerate(
        c.execute(
            "SELECT e.*,r.sequence_number,r.previous_root_hash,r.root_hash "
            "FROM events e JOIN event_chain_roots r ON r.event_id=e.event_id "
            "AND r.subject_id=e.subject_id WHERE e.subject_id=? ORDER BY r.sequence_number",
            (subject,),
        ),
        start=1,
    ):
        expected = EventStore._chain_hash(row, sequence, previous)
        if (
            row["sequence_number"] != sequence
            or row["previous_root_hash"] != previous
            or row["root_hash"] != expected
        ):
            return "event_chain_invalid"
        previous = expected
        verified += 1
    return None if verified == count else "event_chain_invalid"


def inspect(c: sqlite3.Connection, subject: str, *, limit: int = 256) -> tuple[Evidence, ...]:
    if not 1 <= limit <= 4096:
        raise ValueError("survey relationship budget invalid")
    tables = _tables(c)
    rows = c.execute(
        "SELECT * FROM relationships WHERE subject_id=? ORDER BY relationship_id LIMIT ?",
        (subject, limit + 1),
    ).fetchall()
    if len(rows) > limit:
        raise ValueError("survey relationship budget exceeded; do not silently truncate")
    chain_error = _chain(c, subject, tables)
    results = []
    for row in rows:
        reasons: set[str] = set()
        if chain_error:
            reasons.add(chain_error)
        revisions = c.execute(
            "SELECT * FROM relationship_revisions WHERE relationship_id=? ORDER BY "
            "revision_number LIMIT 257",
            (row["relationship_id"],),
        ).fetchall()
        if len(revisions) > 256:
            raise ValueError("survey revision budget exceeded; do not silently truncate")
        if not revisions or revisions[0]["revision_number"] != 1:
            reasons.add("formation_revision_missing")
        evidence: set[tuple[str, str]] = set()
        for revision in revisions:
            try:
                ids = strict_json_loads(revision["source_event_ids_json"])
                if (
                    not isinstance(ids, list)
                    or not ids
                    or len(ids) > 64
                    or any(not isinstance(e, str) for e in ids)
                ):
                    raise ValueError("invalid source list")
            except (TypeError, ValueError):
                reasons.add("source_list_invalid")
                continue
            for event_id in ids:
                event = c.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
                if event is None or event["subject_id"] != subject:
                    reasons.add("source_missing_or_foreign")
                    continue
                if (
                    "payload_archive_key" in set(event.keys())
                    and event["payload_archive_key"] is not None
                ):
                    reasons.add("archived_payload_not_read")
                    continue
                raw = event["payload_json"]
                if not isinstance(raw, str) or len(raw.encode()) > 65536:
                    reasons.add("payload_unavailable")
                    continue
                try:
                    payload = strict_json_loads(raw)
                    if (
                        not isinstance(payload, dict)
                        or content_hash(payload) != event["payload_hash"]
                    ):
                        raise ValueError("payload hash mismatch")
                except (TypeError, ValueError):
                    reasons.add("payload_invalid")
                    continue
                if event["event_type"] != "interaction_received" or not isinstance(
                    payload.get("interaction_id"), str
                ):
                    reasons.add("source_has_no_identity_contract")
                    continue
                iid = payload["interaction_id"]
                if "private_conversation_messages" in tables:
                    private = c.execute(
                        "SELECT c.status FROM private_conversation_messages m JOIN "
                        "private_conversations c ON "
                        "c.conversation_id=m.conversation_id WHERE m.interaction_id=? "
                        "AND c.subject_id=?",
                        (iid, subject),
                    ).fetchone()
                    if private is not None and private[0] in {"deleting", "deleted"}:
                        reasons.add("private_source_withdrawn")
                        continue
                if "participant_message_links" not in tables:
                    reasons.add("legacy_address_without_identity_namespace")
                    continue
                link = c.execute(
                    "SELECT e.entity_type,e.entity_id,e.status,m.created_at AS "
                    "linked_at FROM participant_message_links m JOIN "
                    "participant_entities e ON e.subject_id=m.subject_id AND "
                    "e.entity_id=m.entity_id JOIN participant_sources s ON "
                    "s.subject_id=m.subject_id AND s.entity_id=m.entity_id AND "
                    "s.source_id=m.source_id WHERE m.subject_id=? AND m.interaction_id=?",
                    (subject, iid),
                ).fetchone()
                if link is None:
                    reasons.add("legacy_address_without_identity_namespace")
                    continue
                kind = (
                    "human"
                    if link["entity_type"] == "human"
                    else "agent"
                    if link["entity_type"] in {"agent", "noyra"}
                    else "entity"
                )
                evidence.add((kind, link["entity_id"]))
                if link["status"] != "active":
                    reasons.add("participant_disabled")
                try:
                    if datetime.fromisoformat(link["linked_at"]) > datetime.fromisoformat(
                        row["created_at"]
                    ):
                        reasons.add("attribution_added_after_formation")
                except (TypeError, ValueError):
                    reasons.add("attribution_time_unusable")
        # Social runs bind relationship_id to counterparty at a later time.
        # This is useful conflicting/corroborating evidence, never birth proof.
        matching_snapshots = 0
        if "relationship_social_runs" in tables:
            runs = c.execute(
                "SELECT * FROM relationship_social_runs WHERE relationship_id=? LIMIT 257",
                (row["relationship_id"],),
            ).fetchall()
            if len(runs) > 256:
                raise ValueError("survey social budget exceeded; do not silently truncate")
            for run in runs:
                try:
                    if (
                        max(
                            len(run[key].encode())
                            for key in ("proposal_json", "evidence_event_ids_json")
                        )
                        > 65536
                    ):
                        raise ValueError("snapshot too large")
                    proposal = strict_json_loads(run["proposal_json"])
                    sources = strict_json_loads(run["evidence_event_ids_json"])
                    if not isinstance(proposal, dict) or not isinstance(sources, list):
                        raise ValueError("snapshot shape")
                    expected = content_hash(
                        {
                            key: run[key]
                            for key in (
                                "relationship_id",
                                "model_call_id",
                                "interaction_id",
                                "disposition",
                                "channel",
                                "counterparty",
                                "topic",
                                "rationale",
                                "proposal_hash",
                                "created_at",
                            )
                        }
                        | {"evidence_event_ids": sources}
                    )
                    if (
                        expected != run["state_hash"]
                        or content_hash(proposal) != run["proposal_hash"]
                    ):
                        raise ValueError("snapshot hash")
                except (AttributeError, TypeError, ValueError):
                    reasons.add("social_snapshot_invalid")
                    continue
                if run["subject_id"] != subject or run["counterparty"] != row["entity_key"]:
                    reasons.add("social_snapshot_disagrees")
                else:
                    matching_snapshots += 1
        current = (row["entity_type"], row["entity_key"])
        if evidence and evidence != {current}:
            reasons.add("source_identity_disagrees")
        if len(evidence) > 1:
            reasons.add("multiple_source_entities")
        if {
            "source_identity_disagrees",
            "multiple_source_entities",
            "source_missing_or_foreign",
            "source_list_invalid",
            "payload_invalid",
            "event_chain_invalid",
            "social_snapshot_invalid",
            "social_snapshot_disagrees",
        } & reasons:
            category = "review_required"
        elif evidence == {current} and not reasons:
            category = "correlated_not_proven"
        else:
            category = "unattested"
        results.append(
            Evidence(
                row["relationship_id"],
                subject,
                row["entity_type"],
                row["entity_key"],
                category,
                tuple(sorted(reasons)),
                matching_social_snapshots=matching_snapshots,
            )
        )
    return tuple(results)


def snapshot(c: sqlite3.Connection) -> str:
    """Fingerprint every legacy relation and revision, including non-hashed fields."""
    return content_hash(
        {
            table: [dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("relationships", "relationship_revisions")
        }
    )
