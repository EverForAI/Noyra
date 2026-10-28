"""Compatibility recovery for historical privacy intents; new deletion is disabled."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
from datetime import UTC, datetime
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
_REVIEW_CODES = frozenset(
    {
        "private_erasure_scope_exceeds_limit",
        "private_calls_in_flight",
        "cross_window_derivative_review_required",
        "private_event_derivative_review_required",
        "private_erasure_review_deadline",
        "legacy_private_provenance_review_required",
        "reflection_derivative_review_required",
        "private_memory_derivative_review_required",
        "private_exports_in_flight",
        "private_legacy_export_review_required",
        "multi_source_memory_review_required",
    }
)

# Retained for historical recovery only. Rich/transitive provenance still
# requires review; disabling the feature must not weaken existing receipts.
_UNREVIEWED_DOMAINS = (
    "appraisals",
    "beliefs",
    "goals",
    "relationships",
    "relationship_identity_bindings",
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
    "value_profiles",
    "mission_candidates",
    "motivation_reviews",
    "epistemic_review_runs",
    "goal_governance_runs",
    "thought_agenda_items",
    "self_modification_proposals",
    "workflow_checkpoints",
    "causal_links",
    "event_payload_segments",
    "world_claims",
    "predictions",
)


class ErasureReviewRequired(RuntimeError):
    """An opaque historical derivative cannot safely be treated as cleared."""


class ConversationErasure:
    def __init__(self, database: Database):
        self.database = database
        self.root = database.path.parent / "privacy-deletions"

    def assess(self, subject_id: str, conversation_id: str) -> dict[str, Any]:
        """Explain the first prototype blocker without accepting a deletion intent."""
        counts = None
        with self.database.read_transaction() as connection:
            connection.execute("PRAGMA query_only=ON")
            state = _row(connection, subject_id, conversation_id)
            try:
                messages, calls, events = self._review(connection, subject_id, conversation_id)
            except ErasureReviewRequired as error:
                reason = str(error)
                if reason not in _REVIEW_CODES:
                    raise IntegrityError("private erasure assessment reason is invalid") from error
                status = "review_required"
            else:
                reason = "private_erasure_full_contract_pending"
                status = "full_contract_pending"
                counts = {
                    "messages": len(messages),
                    "model_copies": len(calls),
                    "events": len(events),
                }
        return {
            "status": status,
            "deletion_available": False,
            "reason_code": reason,
            "conversation_revision": state["revision"],
            "counts": counts,
            "checked_at": utc_now(),
            "coverage": "first_prototype_blocker",
        }

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
        """Do not accept a new deletion, including through the former internal API."""
        raise ErasureReviewRequired("private_deletion_disabled")

    def _resume_recorded_intent(
        self, subject_id: str, conversation_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        marker = _read_marker(self.root / (content_hash([subject_id, conversation_id]) + ".json"))
        if marker["subject_id"] != subject_id or marker["conversation_id"] != conversation_id:
            raise IntegrityError("privacy deletion recovery subject mismatch")
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
                job_id = marker["job_id"]
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
            elif existing["job_id"] != marker["job_id"]:
                raise IntegrityError("privacy deletion job and journal mismatch")
        result = self.get(subject_id, conversation_id)
        assert result is not None
        return result

    def resume(self, subject_id: str, *, limit: int = 16) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid erasure recovery limit")
        markers = _journal_markers(self.root)
        with self.database.connection() as connection:
            jobs = connection.execute(
                "SELECT job_id,conversation_id,status FROM private_erasure_jobs "
                "WHERE subject_id=? ORDER BY created_at,job_id LIMIT 10001",
                (subject_id,),
            ).fetchall()
        if len(jobs) > 10_000:
            raise IntegrityError("privacy journal exceeds recovery limit")
        pending = {
            window: marker for (subject, window), marker in markers.items() if subject == subject_id
        }
        for job in jobs:
            actual = self._write_marker(subject_id, job["conversation_id"], job["job_id"])
            if actual != job["job_id"]:
                raise IntegrityError("privacy deletion job and journal mismatch")
            if job["status"] == "completed":
                pending.pop(job["conversation_id"], None)
            else:
                pending[job["conversation_id"]] = {
                    "subject_id": subject_id,
                    "conversation_id": job["conversation_id"],
                    "job_id": job["job_id"],
                }
        results: list[dict[str, Any]] = []
        # Journal entries can outlive a conversation (for example after an
        # already completed restore). They must not consume the bounded
        # recovery budget or starve real pending jobs later in the journal.
        for window, marker in pending.items():
            if len(results) >= limit:
                break
            with self.database.connection() as connection:
                state = connection.execute(
                    "SELECT revision FROM private_conversations WHERE subject_id=? "
                    "AND conversation_id=?",
                    (subject_id, window),
                ).fetchone()
            if state is None:
                continue
            job = self._resume_recorded_intent(subject_id, window, expected_revision=state[0])
            if job["job_id"] != marker["job_id"]:
                raise IntegrityError("privacy deletion recovery identity mismatch")
            results.append(self.run(subject_id, window))
        return results

    def run(self, subject_id: str, conversation_id: str) -> dict[str, Any]:
        result = self.get(subject_id, conversation_id)
        if result is None:
            raise NotFoundError("erasure job not found")
        if result["status"] == "completed":
            return result
        try:
            retired_exports = self._retire_exports(subject_id)
            with self.database.transaction() as connection:
                messages, calls, events = self._review(connection, subject_id, conversation_id)
                connection.execute(
                    "UPDATE private_erasure_jobs SET status='running',stage='content',"
                    "error_code=NULL,updated_at=? WHERE job_id=?",
                    (utc_now(), result["job_id"]),
                )
                counts = {
                    "messages": len(messages),
                    "model_copies": len(calls),
                    "retired_exports": retired_exports,
                }
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
                counts.update(
                    self._erase_affected_memories(connection, result["job_id"], subject_id, events)
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
                self._verify(connection, subject_id, conversation_id, messages, calls, events)
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
    def _verify(
        connection: Any,
        subject: str,
        window: str,
        messages: list[Any],
        calls: list[Any],
        events: set[str],
    ) -> None:
        state = _row(connection, subject, window)
        if state["title"] != ERASED or state["status"] != "deleted" or state["pinned"]:
            raise IntegrityError("private erasure window residual")
        for original in messages:
            message = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id=? AND subject_id=?",
                (original["interaction_id"], subject),
            ).fetchone()
            if message is None or message["content"] != ERASED or message["rationale"] is not None:
                raise IntegrityError("private erasure message residual")
            InteractionStore._from_row(message)
            if connection.execute(
                "SELECT 1 FROM interaction_decisions WHERE interaction_id=? AND rationale!=?",
                (original["interaction_id"], ERASED),
            ).fetchone():
                raise IntegrityError("private erasure decision residual")
        for original in calls:
            call = connection.execute(
                "SELECT request_json,response_json,response_hash FROM model_calls "
                "WHERE call_id=? AND subject_id=?",
                (original["call_id"], subject),
            ).fetchone()
            if (
                call is None
                or call["request_json"] is not None
                or call["response_json"] != canonical_json(ERASED_MODEL)
                or call["response_hash"] != content_hash(ERASED_MODEL)
            ):
                raise IntegrityError("private erasure model residual")
        for revision in connection.execute(
            "SELECT r.memory_id,r.content,r.reason,r.source_event_ids_json,m.content AS current,"
            "m.status FROM memory_revisions r JOIN memories m ON m.memory_id=r.memory_id "
            "WHERE m.subject_id=? LIMIT ?",
            (subject, MAX_ERASURE_ROWS + 1),
        ).fetchall():
            if not set(_memory_sources(revision["source_event_ids_json"])) & events:
                continue
            if (
                revision["content"] != ERASED
                or revision["reason"] != "privacy erasure"
                or revision["current"] != ERASED
                or revision["status"] != "archived"
            ):
                raise IntegrityError("private erasure memory residual")
            if connection.execute(
                "SELECT 1 FROM memory_embeddings WHERE memory_id=?", (revision["memory_id"],)
            ).fetchone():
                raise IntegrityError("private erasure index residual")

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
            if connection.execute(
                "SELECT 1 FROM private_model_sources other JOIN private_model_sources affected "
                "ON affected.call_id=other.call_id WHERE affected.subject_id=? "
                "AND affected.conversation_id=? AND other.conversation_id!=? LIMIT 1",
                (subject, window, window),
            ).fetchone():
                raise ErasureReviewRequired("cross_window_derivative_review_required")
            for event in events:
                if connection.execute(
                    "SELECT 1 FROM events e,json_each(e.causal_parent_ids_json) parent "
                    "WHERE e.subject_id=? AND parent.value=? LIMIT 1",
                    (subject, event),
                ).fetchone():
                    raise ErasureReviewRequired("private_event_derivative_review_required")
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
            if (
                table == "relationship_identity_bindings"
                and not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
            ):
                # Historical privacy recovery can run before schema 107.
                # Missing current-schema evidence is still an integrity error.
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                try:
                    historical_version = int(version[0]) if version is not None else 0
                except (TypeError, ValueError):
                    historical_version = 0
                if 1 <= historical_version < 107:
                    continue
                raise IntegrityError("relationship identity storage is unavailable")
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
        ConversationErasure._review_exports(connection, subject)

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
        for memory_id, sources in sources_by_memory.items():
            if sources & events and not sources <= events:
                ConversationErasure._independent_memory_revision(connection, memory_id, events)

    @staticmethod
    def _review_exports(connection: Any, subject: str) -> None:
        if connection.execute(
            "SELECT 1 FROM export_jobs WHERE subject_id=? AND status IN ('queued','running') "
            "LIMIT 1",
            (subject,),
        ).fetchone():
            raise ErasureReviewRequired("private_exports_in_flight")
        # Legacy exports can have arbitrary destinations with no job ownership.
        # Retiring the managed job directory cannot prove those copies are gone.
        if connection.execute(
            "SELECT 1 FROM training_exports WHERE subject_id=? LIMIT 1",
            (subject,),
        ).fetchone():
            raise ErasureReviewRequired("private_legacy_export_review_required")

    def _retire_exports(self, subject: str) -> int:
        from noyra.core.at_rest import _contains_reparse_component

        root = self.database.path.parent / "exports" / "jobs"
        with self.database.transaction() as connection:
            self._review_exports(connection, subject)
            rows = connection.execute(
                "SELECT job_id,artifact_path FROM export_jobs WHERE subject_id=? LIMIT ?",
                (subject, MAX_ERASURE_ROWS + 1),
            ).fetchall()
            if len(rows) > MAX_ERASURE_ROWS:
                raise ErasureReviewRequired("private_erasure_scope_exceeds_limit")
            # Persist retirement before unlinking. Readers reject this state,
            # and a crash leaves enough ownership metadata to retry cleanup.
            for row in rows:
                connection.execute(
                    "UPDATE export_jobs SET error_code='privacy_erasure_retiring' "
                    "WHERE job_id=? AND subject_id=?",
                    (row["job_id"], subject),
                )
        if rows and _contains_reparse_component(root):
            raise IntegrityError("private export root is unsafe")
        for row in rows:
            expected = f"{row['job_id']}.zip"
            saved = Path(row["artifact_path"]) if row["artifact_path"] else None
            if re.fullmatch(r"export-job_[a-f0-9]{32}", row["job_id"]) is None or (
                saved is not None
                and (
                    saved.name != expected
                    or saved.parent.name != "jobs"
                    or saved.parent.parent.name != "exports"
                )
            ):
                raise ErasureReviewRequired("private_unmanaged_export_review_required")
            # Backups retain the old absolute path. Always clean the owned
            # artifact in this data root, never follow the stored destination.
            candidates = [root / expected]
            if root.exists():
                for index, candidate in enumerate(root.iterdir()):
                    if index >= MAX_ERASURE_ROWS:
                        raise ErasureReviewRequired("private_erasure_scope_exceeds_limit")
                    if candidate.name.startswith(f".{expected}.") and candidate.name.endswith(
                        (".tmp", ".pending")
                    ):
                        candidates.append(candidate)
            for target in candidates:
                if not target.exists() and not target.is_symlink():
                    continue
                metadata = target.lstat()
                if (
                    _contains_reparse_component(target)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                ):
                    raise IntegrityError("private export artifact is unsafe")
                target.unlink()
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE export_jobs SET artifact_path=NULL,filename=NULL,byte_size=NULL,"
                    "error_code='privacy_erasure_retired' WHERE job_id=? AND subject_id=?",
                    (row["job_id"], subject),
                )
        return len(rows)

    @staticmethod
    def _independent_memory_revision(connection: Any, memory_id: str, events: set[str]) -> Any:
        earliest = min(
            _timestamp(
                connection.execute(
                    "SELECT observed_at FROM events WHERE event_id=?", (event,)
                ).fetchone()[0]
            )
            for event in events
        )
        # A later revision may copy older private content despite citing a new
        # event. Only a version recorded before any withdrawn input is evidence.
        for revision in connection.execute(
            "SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY revision_number DESC",
            (memory_id,),
        ).fetchall():
            sources = set(_memory_sources(revision["source_event_ids_json"]))
            created_at = _timestamp(revision["created_at"])
            if sources and not sources & events and created_at < earliest:
                independent_sources = connection.execute(
                    "SELECT e.event_id,e.observed_at FROM events e "
                    "JOIN memories m ON m.subject_id=e.subject_id "
                    "WHERE m.memory_id=? AND e.event_id IN ("
                    + ",".join("?" for _ in sources)
                    + ")",
                    (memory_id, *sources),
                ).fetchall()
                if len(independent_sources) != len(sources) or any(
                    _timestamp(source["observed_at"]) > created_at for source in independent_sources
                ):
                    continue
                from noyra.mind.memory import MemoryStore

                if revision["content_hash"] != content_hash(revision["content"]) or revision[
                    "state_hash"
                ] != MemoryStore._state_hash(
                    revision["content"],
                    float(revision["salience"]),
                    float(revision["confidence"]),
                    revision["status"],
                ):
                    raise IntegrityError("independent memory revision content mismatch")
                return revision
        raise ErasureReviewRequired("multi_source_memory_review_required")

    def _erase_affected_memories(
        self, connection: Any, job_id: str, subject: str, events: set[str]
    ) -> dict[str, int]:
        erased = rebuilt = 0
        for memory in connection.execute(
            "SELECT memory_id,current_revision,content_hash,salience,confidence,"
            "memory_type,privacy_level,status "
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
            if not sources & events:
                continue
            if not sources <= events:
                independent = ConversationErasure._independent_memory_revision(
                    connection, memory["memory_id"], events
                )
                from noyra.mind.memory import MemoryStore

                rebuilder = MemoryStore(self.database)
                replacement = rebuilder._create_connection(
                    connection,
                    subject,
                    memory["memory_type"],
                    independent["content"],
                    salience=float(independent["salience"]),
                    confidence=float(independent["confidence"]),
                    source_event_ids=_memory_sources(independent["source_event_ids_json"]),
                    privacy_level=memory["privacy_level"],
                    reason="independent memory reconstruction after privacy erasure",
                )
                if independent["status"] == "archived" or memory["status"] == "archived":
                    # Reconstructed archived facts must not become active knowledge.
                    rebuilder._revise_connection(
                        connection,
                        replacement.memory_id,
                        content=independent["content"],
                        salience=float(independent["salience"]),
                        confidence=float(independent["confidence"]),
                        status="archived",
                        reason="preserve independent archived state",
                        source_event_ids=_memory_sources(independent["source_event_ids_json"]),
                    )
                rebuilt += 1
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
            erased += int(sources <= events)
        return {"exclusive_memories": erased, "reconstructed_memories": rebuilt}

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


def _timestamp(value: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timezone missing")
        return timestamp.astimezone(UTC)
    except (TypeError, ValueError) as error:
        raise IntegrityError("private provenance timestamp is invalid") from error


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
    from noyra.core.database import Database

    markers: dict[tuple[str, str], dict[str, str]] = {}
    for root in (restored_root / "privacy-deletions", current_root / "privacy-deletions"):
        markers.update(_journal_markers(root))
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
        erasure._resume_recorded_intent(subject, window, expected_revision=row[0])
        if erasure.run(subject, window)["status"] != "completed":
            raise ErasureReviewRequired("backup_private_deletion_replay_requires_review")


def _journal_markers(root: Path) -> dict[tuple[str, str], dict[str, str]]:
    from noyra.core.at_rest import _contains_reparse_component

    if not root.exists() and not root.is_symlink():
        return {}
    if _contains_reparse_component(root) or not root.is_dir():
        raise IntegrityError("privacy journal root is unsafe")
    markers = {}
    for index, path in enumerate(root.iterdir()):
        if index >= 10_000:
            raise IntegrityError("privacy journal exceeds recovery limit")
        marker = _read_marker(path)
        markers[(marker["subject_id"], marker["conversation_id"])] = marker
    return markers
