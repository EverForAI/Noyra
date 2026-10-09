"""Durable privacy deletion with an explicit stop on unreviewed provenance."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

from .conversations import ConversationConflict, _row
from .store import InteractionStore

if TYPE_CHECKING:
    from noyra.core.database import Database

ERASED = "[erased]"
ERASED_MODEL = {"privacy_erased": True}
MAX_ERASURE_ROWS = 20_000
MAX_REVIEW_SECONDS = 5

# This foundation deliberately excludes rich/transitive provenance. It has no
# public endpoint until each domain has its own reviewed deletion contract.
_UNREVIEWED_DOMAINS = (
    "appraisals",
    "beliefs",
    "goals",
    "relationships",
    "self_models",
    "thought_episodes",
    "relationship_social_runs",
    "psychological_snapshots",
    "memory_integrations",
    "memory_consolidation_runs",
    "memory_blocks",
    "entities",
    "entity_relations",
    "entity_evidence_links",
    "training_exports",
    "public_diary_entries",
    "consciousness_frames",
    "actions",
    "autonomous_projects",
    "personality_candidates",
    "observations",
    "state_snapshots",
    "snapshot_archives",
    "storage_archives",
    "archive_transfer_queue",
)


class ErasureReviewRequired(RuntimeError):
    """An opaque historical derivative cannot safely be treated as cleared."""


class ConversationErasure:
    def __init__(self, database: Database):
        self.database = database
        self.root = database.path.parent / "privacy-deletions"

    def get(self, subject_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            _row(connection, subject_id, conversation_id)
            row = connection.execute(
                "SELECT * FROM private_erasure_jobs WHERE subject_id=? AND conversation_id=?",
                (subject_id, conversation_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["counts"] = json.loads(result.pop("counts_json"))
        return result

    def request(
        self, subject_id: str, conversation_id: str, *, expected_revision: int, confirmed: bool
    ) -> dict[str, Any]:
        if confirmed is not True or type(expected_revision) is not int:
            raise ValueError("explicit deletion confirmation is required")
        with self.database.transaction() as connection:
            state = _row(connection, subject_id, conversation_id)
            existing = connection.execute(
                "SELECT job_id FROM private_erasure_jobs WHERE subject_id=? AND conversation_id=?",
                (subject_id, conversation_id),
            ).fetchone()
            if existing is None:
                if state["revision"] != expected_revision:
                    raise ConversationConflict("conversation revision changed")
                self._review(connection, subject_id, conversation_id)
                job_id = new_id("erase")
                # Persist outside the database before accepting the intent. A crash before
                # the transaction commits still leaves a deletion intent to replay.
                job_id = self._write_marker(subject_id, conversation_id, job_id)
                state.update(
                    status="deleting", revision=state["revision"] + 1, updated_at=utc_now()
                )
                connection.execute(
                    "UPDATE private_conversations SET status=?,revision=?,updated_at=?,"
                    "state_hash=? "
                    "WHERE conversation_id=? AND subject_id=?",
                    (
                        state["status"],
                        state["revision"],
                        state["updated_at"],
                        content_hash(state),
                        conversation_id,
                        subject_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO private_erasure_jobs "
                    "VALUES(?,?,?,'pending','fence',NULL,'{}',?,?)",
                    (job_id, subject_id, conversation_id, utc_now(), utc_now()),
                )
                connection.execute(
                    "INSERT INTO audit_records VALUES(?,?, 'private_erasure_requested',"
                    "'operator',?,?)",
                    (new_id("audit"), subject_id, canonical_json({"job_id": job_id}), utc_now()),
                )
        result = self.get(subject_id, conversation_id)
        assert result is not None
        return result

    def run(self, subject_id: str, conversation_id: str) -> dict[str, Any]:
        result = self.get(subject_id, conversation_id)
        if result is None:
            raise NotFoundError("erasure job not found")
        if result["status"] == "completed":
            return result
        try:
            with self.database.transaction() as connection:
                messages, calls, events = self._review(connection, subject_id, conversation_id)
                connection.execute(
                    "UPDATE private_erasure_jobs SET status='running',stage='content',"
                    "error_code=NULL,updated_at=? WHERE job_id=?",
                    (utc_now(), result["job_id"]),
                )
                counts = {"messages": len(messages), "model_copies": len(calls)}
                for row in messages:
                    digest = InteractionStore._state_hash_values(
                        ERASED,
                        row["direction"],
                        row["kind"],
                        row["channel"],
                        row["counterparty"],
                        row["status"],
                        None,
                    )
                    self._receipt(
                        connection,
                        result["job_id"],
                        "interactions",
                        row["interaction_id"],
                        row["content_hash"],
                        digest,
                    )
                    connection.execute(
                        "UPDATE interactions SET content=?,content_hash=?,rationale=NULL,"
                        "state_hash=? "
                        "WHERE interaction_id=?",
                        (ERASED, content_hash(ERASED), digest, row["interaction_id"]),
                    )
                    for decision in connection.execute(
                        "SELECT * FROM interaction_decisions WHERE interaction_id=?",
                        (row["interaction_id"],),
                    ).fetchall():
                        state = {
                            name: decision[name]
                            for name in ("from_status", "to_status", "rationale", "actor")
                        }
                        state["rationale"] = ERASED
                        digest = content_hash(state)
                        self._receipt(
                            connection,
                            result["job_id"],
                            "interaction_decisions",
                            decision["decision_id"],
                            decision["state_hash"],
                            digest,
                        )
                        connection.execute(
                            "UPDATE interaction_decisions SET rationale=?,state_hash=? "
                            "WHERE decision_id=?",
                            (ERASED, digest, decision["decision_id"]),
                        )
                for row in calls:
                    self._receipt(
                        connection,
                        result["job_id"],
                        "model_calls",
                        row["call_id"],
                        row["request_hash"],
                        content_hash(ERASED_MODEL),
                    )
                    connection.execute(
                        "UPDATE model_calls SET request_json=NULL,response_json=?,response_hash=? "
                        "WHERE call_id=?",
                        (canonical_json(ERASED_MODEL), content_hash(ERASED_MODEL), row["call_id"]),
                    )
                counts["exclusive_memories"] = self._erase_exclusive_memories(
                    connection, result["job_id"], subject_id, events
                )
                state = _row(connection, subject_id, conversation_id)
                state.update(
                    title=ERASED,
                    pinned=0,
                    status="deleted",
                    revision=state["revision"] + 1,
                    updated_at=utc_now(),
                )
                connection.execute(
                    "UPDATE private_conversations SET title=?,pinned=?,status=?,revision=?,"
                    "updated_at=?,state_hash=? WHERE conversation_id=? AND subject_id=?",
                    (
                        ERASED,
                        0,
                        "deleted",
                        state["revision"],
                        state["updated_at"],
                        content_hash(state),
                        conversation_id,
                        subject_id,
                    ),
                )
                connection.execute(
                    "UPDATE private_erasure_jobs SET status='completed',stage='verified',"
                    "counts_json=?,updated_at=? WHERE job_id=?",
                    (canonical_json(counts), utc_now(), result["job_id"]),
                )
        except (ErasureReviewRequired, OSError, IntegrityError, sqlite3.DatabaseError) as error:
            code = (
                str(error) if isinstance(error, ErasureReviewRequired) else "erasure_storage_error"
            )
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE private_erasure_jobs SET status='failed',stage='review',error_code=?,"
                    "updated_at=? WHERE job_id=?",
                    (code, utc_now(), result["job_id"]),
                )
        final = self.get(subject_id, conversation_id)
        assert final is not None
        return final

    @staticmethod
    def _receipt(connection: Any, job: str, table: str, key: str, old: str, new: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO private_erasure_receipts VALUES(?,?,?,?,?)",
            (job, table, key, old, new),
        )

    def _review(
        self, connection: Any, subject: str, window: str
    ) -> tuple[list[Any], list[Any], set[str]]:
        deadline = time.monotonic() + MAX_REVIEW_SECONDS
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            messages = connection.execute(
                "SELECT i.interaction_id,i.content_hash,i.direction,i.kind,i.channel,"
                "i.counterparty,i.status FROM interactions i JOIN private_conversation_messages m "
                "ON i.interaction_id=m.interaction_id WHERE m.subject_id=? "
                "AND m.conversation_id=? AND i.subject_id=? LIMIT ?",
                (subject, window, subject, MAX_ERASURE_ROWS + 1),
            ).fetchall()
            calls = connection.execute(
                "SELECT DISTINCT c.call_id,c.request_hash,c.purpose,c.status "
                "FROM model_calls c JOIN private_model_sources s "
                "ON s.call_id=c.call_id WHERE s.subject_id=? AND s.conversation_id=? "
                "AND c.subject_id=? LIMIT ?",
                (subject, window, subject, MAX_ERASURE_ROWS + 1),
            ).fetchall()
            events = {
                row[0]
                for row in connection.execute(
                    "SELECT e.event_id FROM events e JOIN private_conversation_messages m "
                    "ON json_extract(e.payload_json,'$.interaction_id')=m.interaction_id "
                    "WHERE m.subject_id=? AND m.conversation_id=? AND e.subject_id=? "
                    "AND e.event_type IN "
                    "('interaction_received','interaction_sent','interaction_decision') LIMIT ?",
                    (subject, window, subject, MAX_ERASURE_ROWS + 1),
                )
            }
            if max(len(messages), len(calls), len(events)) > MAX_ERASURE_ROWS:
                raise ErasureReviewRequired("private_erasure_scope_exceeds_limit")
            if any(row["status"] in {"prepared", "executing"} for row in calls):
                raise ErasureReviewRequired("private_calls_in_flight")
            self._check_derivatives(connection, subject, events, calls)
            return messages, calls, events
        except sqlite3.OperationalError as error:
            if time.monotonic() > deadline:
                raise ErasureReviewRequired("private_erasure_review_deadline") from error
            raise
        finally:
            connection.set_progress_handler(None, 0)

    @staticmethod
    def _check_derivatives(
        connection: Any, subject: str, events: set[str], calls: list[Any]
    ) -> None:
        # Historical model calls did not record exact private input lineage. Require
        # review instead of guessing that an opaque reflection has no private text.
        if connection.execute(
            "SELECT 1 FROM model_calls WHERE subject_id=? AND "
            "NOT EXISTS(SELECT 1 FROM private_model_sources s "
            "WHERE s.call_id=model_calls.call_id) LIMIT 1",
            (subject,),
        ).fetchone():
            raise ErasureReviewRequired("legacy_private_provenance_review_required")
        if any(row["purpose"].startswith("sleep_reflection:") for row in calls):
            raise ErasureReviewRequired("reflection_derivative_review_required")
        for table in _UNREVIEWED_DOMAINS:
            if connection.execute(
                f'SELECT 1 FROM "{table}" WHERE subject_id=? LIMIT 1', (subject,)
            ).fetchone():
                raise ErasureReviewRequired("private_memory_derivative_review_required")
        if connection.execute(
            "SELECT 1 FROM sleep_reflections r JOIN sleep_runs s ON s.sleep_id=r.sleep_id "
            "WHERE s.subject_id=? LIMIT 1",
            (subject,),
        ).fetchone():
            raise ErasureReviewRequired("reflection_derivative_review_required")
        if connection.execute(
            "SELECT 1 FROM export_jobs WHERE subject_id=? LIMIT 1", (subject,)
        ).fetchone():
            raise ErasureReviewRequired("private_export_cleanup_required")

        revisions = connection.execute(
            "SELECT r.memory_id,r.source_event_ids_json FROM memory_revisions r "
            "JOIN memories m ON m.memory_id=r.memory_id WHERE m.subject_id=? LIMIT ?",
            (subject, MAX_ERASURE_ROWS + 1),
        ).fetchall()
        if len(revisions) > MAX_ERASURE_ROWS:
            raise ErasureReviewRequired("private_erasure_scope_exceeds_limit")
        sources_by_memory: dict[str, set[str]] = {}
        for revision in revisions:
            sources_by_memory.setdefault(revision["memory_id"], set()).update(
                _memory_sources(revision["source_event_ids_json"])
            )
        for sources in sources_by_memory.values():
            if sources & events and not sources <= events:
                raise ErasureReviewRequired("multi_source_memory_review_required")

    @staticmethod
    def _erase_exclusive_memories(
        connection: Any, job_id: str, subject: str, events: set[str]
    ) -> int:
        erased = 0
        for memory in connection.execute(
            "SELECT memory_id,current_revision,content_hash,salience,confidence "
            "FROM memories WHERE subject_id=? LIMIT ?",
            (subject, MAX_ERASURE_ROWS + 1),
        ).fetchall():
            revisions = connection.execute(
                "SELECT revision_id,source_event_ids_json,content_hash,salience,confidence,status "
                "FROM memory_revisions WHERE memory_id=?",
                (memory["memory_id"],),
            ).fetchall()
            sources = {
                source
                for revision in revisions
                for source in _memory_sources(revision["source_event_ids_json"])
            }
            if not sources or not sources <= events:
                continue
            for revision in revisions:
                digest = content_hash(ERASED)
                state_hash = content_hash(
                    {
                        "content": ERASED,
                        "salience": float(revision["salience"]),
                        "confidence": float(revision["confidence"]),
                        "status": revision["status"],
                    }
                )
                ConversationErasure._receipt(
                    connection,
                    job_id,
                    "memory_revisions",
                    revision["revision_id"],
                    revision["content_hash"],
                    state_hash,
                )
                connection.execute(
                    "UPDATE memory_revisions SET content=?,content_hash=?,state_hash=?,reason=? "
                    "WHERE revision_id=?",
                    (ERASED, digest, state_hash, "privacy erasure", revision["revision_id"]),
                )
            current_hash = content_hash(ERASED)
            from noyra.mind.memory import MemoryStore

            MemoryStore._insert_revision(
                connection,
                memory["memory_id"],
                int(memory["current_revision"]) + 1,
                ERASED,
                current_hash,
                float(memory["salience"]),
                float(memory["confidence"]),
                "archived",
                "privacy erasure",
                tuple(sorted(sources)),
                utc_now(),
            )
            current_state = content_hash(
                {
                    "content": ERASED,
                    "salience": float(memory["salience"]),
                    "confidence": float(memory["confidence"]),
                    "status": "archived",
                }
            )
            ConversationErasure._receipt(
                connection,
                job_id,
                "memories",
                memory["memory_id"],
                memory["content_hash"],
                current_state,
            )
            connection.execute(
                "UPDATE memories SET content=?,content_hash=?,state_hash=?,updated_at=?,"
                "status='archived',current_revision=current_revision+1 "
                "WHERE memory_id=?",
                (ERASED, current_hash, current_state, utc_now(), memory["memory_id"]),
            )
            if connection.execute(
                "SELECT 1 FROM memory_embeddings WHERE memory_id=?", (memory["memory_id"],)
            ).fetchone():
                ConversationErasure._receipt(
                    connection,
                    job_id,
                    "memory_embeddings",
                    memory["memory_id"],
                    memory["memory_id"],
                    "deleted",
                )
                connection.execute(
                    "DELETE FROM memory_embeddings WHERE memory_id=?", (memory["memory_id"],)
                )
            erased += 1
        return erased

    def _write_marker(self, subject: str, window: str, job: str) -> str:
        from noyra.core.at_rest import _contains_reparse_component, _fsync_directory

        self.root.mkdir(mode=0o700, exist_ok=True)
        metadata = self.root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or _contains_reparse_component(self.root):
            raise IntegrityError("invalid privacy journal")
        path = self.root / (content_hash([subject, window]) + ".json")
        value = canonical_json(
            {
                "format": "noyra-private-deletion/v1",
                "subject_id": subject,
                "conversation_id": window,
                "job_id": job,
            }
        )
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            existing = _read_marker(path)
            if existing["subject_id"] != subject or existing["conversation_id"] != window:
                raise IntegrityError("privacy journal conflict") from None
            return str(existing["job_id"])
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(self.root)
        _fsync_directory(self.root.parent)
        return job


def _memory_sources(value: str) -> tuple[str, ...]:
    if len(value) > 10_000:
        raise IntegrityError("memory source list exceeds limit")
    try:
        sources = json.loads(value)
    except ValueError as error:
        raise IntegrityError("memory source list is invalid") from error
    if (
        not isinstance(sources, list)
        or not 1 <= len(sources) <= 64
        or not all(isinstance(item, str) and 0 < len(item) <= 128 for item in sources)
        or len(set(sources)) != len(sources)
    ):
        raise IntegrityError("memory source list is invalid")
    return tuple(sources)


def _read_marker(path: Path) -> dict[str, str]:
    from noyra.core.at_rest import _contains_reparse_component

    metadata = path.lstat()
    if (
        _contains_reparse_component(path)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > 4096
    ):
        raise IntegrityError("privacy deletion marker is unsafe")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
            raise IntegrityError("privacy deletion marker changed")
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise IntegrityError("privacy deletion marker exceeds limit")
    try:
        marker = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise IntegrityError("privacy deletion marker is invalid") from error
    if (
        not isinstance(marker, dict)
        or set(marker) != {"format", "subject_id", "conversation_id", "job_id"}
        or marker["format"] != "noyra-private-deletion/v1"
        or not all(isinstance(value, str) and 0 < len(value) <= 256 for value in marker.values())
        or re.fullmatch(r"erase_[a-f0-9]{32}", marker["job_id"]) is None
        or path.name != content_hash([marker["subject_id"], marker["conversation_id"]]) + ".json"
    ):
        raise IntegrityError("privacy deletion marker identity mismatch")
    return marker


def replay_privacy_deletions(current_root: Path, restored_root: Path) -> None:
    from noyra.core.at_rest import _contains_reparse_component
    from noyra.core.database import Database

    markers: dict[tuple[str, str], dict[str, str]] = {}
    for root in (restored_root / "privacy-deletions", current_root / "privacy-deletions"):
        if not root.exists() and not root.is_symlink():
            continue
        if _contains_reparse_component(root) or not root.is_dir():
            raise IntegrityError("privacy journal root is unsafe")
        for index, path in enumerate(root.iterdir()):
            if index >= 10_000:
                raise IntegrityError("privacy journal exceeds restore limit")
            marker = _read_marker(path)
            markers[(marker["subject_id"], marker["conversation_id"])] = marker
    if not markers:
        return
    database = Database(restored_root / "noyra.sqlite3")
    erasure = ConversationErasure(database)
    for (subject, window), marker in markers.items():
        erasure._write_marker(subject, window, marker["job_id"])
        with database.connection() as connection:
            row = connection.execute(
                "SELECT revision FROM private_conversations "
                "WHERE subject_id=? AND conversation_id=?",
                (subject, window),
            ).fetchone()
        if row is None:
            continue
        erasure.request(subject, window, expected_revision=row[0], confirmed=True)
        if erasure.run(subject, window)["status"] != "completed":
            raise ErasureReviewRequired("backup_private_deletion_replay_requires_review")
