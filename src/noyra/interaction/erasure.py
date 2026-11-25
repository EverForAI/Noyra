"""Compatibility recovery for historical privacy intents; new deletion is disabled."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from noyra.core.errors import IntegrityError, NotFoundError, PayloadLimitError
from noyra.core.payload_codec import decompress_text
from noyra.core.private_erasure_schema import ERASED_REFLECTION_HASH, ERASED_REFLECTION_PLAN
from noyra.core.types import canonical_json, content_hash, new_id, strict_json_loads, utc_now

from .conversations import ConversationConflict, _row
from .reflection_erasure_scope import sleep_reflection_scope
from .store import InteractionStore

if TYPE_CHECKING:
    from noyra.core.database import Database
    from noyra.sleep.types import SleepMemory

ERASED = "[erased]"
ERASED_MODEL = {"privacy_erased": True}
MAX_ERASURE_ROWS = 20_000
MAX_REVIEW_SECONDS = 5
MAX_REFLECTION_BYTES = 8_000_000
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


@dataclass(frozen=True, slots=True)
class _ErasureScope:
    messages: list[sqlite3.Row]
    calls: list[sqlite3.Row]
    events: set[str]
    memory_ids: set[str]
    reflection_hashes: dict[str, str]
    sleep_memory_links: dict[str, sqlite3.Row]
    memory_source_events: set[str]


class ConversationErasure:
    def __init__(self, database: Database):
        self.database = database
        self.root = database.path.parent / "privacy-deletions"

    def assess(self, subject_id: str, conversation_id: str) -> dict[str, Any]:
        """Explain the first prototype blocker without accepting a deletion intent."""
        counts = None
        deadline = time.monotonic() + MAX_REVIEW_SECONDS
        with self.database.read_transaction() as connection:
            connection.execute("PRAGMA query_only=ON")
            state = _row(connection, subject_id, conversation_id)
            try:
                scope = self._review(connection, subject_id, conversation_id, deadline=deadline)
            except ErasureReviewRequired as error:
                reason = str(error)
                if reason not in _REVIEW_CODES:
                    raise IntegrityError("private erasure assessment reason is invalid") from error
                status = "review_required"
            else:
                reason = "private_erasure_full_contract_pending"
                status = "full_contract_pending"
                counts = {
                    "messages": len(scope.messages),
                    "model_copies": len(scope.calls),
                    "events": len(scope.events),
                }
            reflection_scope = sleep_reflection_scope(
                connection,
                subject_id,
                conversation_id,
                deadline,
                row_limit=MAX_ERASURE_ROWS,
                database=self.database,
            )
        return {
            "status": status,
            "deletion_available": False,
            "reason_code": reason,
            "conversation_revision": state["revision"],
            "counts": counts,
            "sleep_reflection_scope": reflection_scope,
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
                scope = self._review(connection, subject_id, conversation_id)
                connection.execute(
                    "UPDATE private_erasure_jobs SET status='running',stage='content',"
                    "error_code=NULL,updated_at=? WHERE job_id=?",
                    (utc_now(), result["job_id"]),
                )
                counts = {
                    "messages": len(scope.messages),
                    "model_copies": len(scope.calls),
                    "retired_exports": retired_exports,
                }
                if scope.reflection_hashes:
                    connection.execute("PRAGMA secure_delete=ON")
                    counts["sleep_reflections"] = len(scope.reflection_hashes)
                for row in scope.messages:
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
                for row in scope.calls:
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
                for reflection_id, original_hash in scope.reflection_hashes.items():
                    self._receipt(
                        connection,
                        result["job_id"],
                        "sleep_reflections",
                        reflection_id,
                        original_hash,
                        ERASED_REFLECTION_HASH,
                    )
                    connection.execute(
                        "UPDATE sleep_reflections SET summary=?,facts_json='[]',"
                        "contradictions_json='[]',prediction_errors_json='[]',"
                        "unresolved_questions_json='[]',public_diary_candidate=NULL,"
                        "plan_json=?,plan_hash=? WHERE reflection_id=?",
                        (ERASED, ERASED_REFLECTION_PLAN, ERASED_REFLECTION_HASH, reflection_id),
                    )
                if scope.sleep_memory_links:
                    # Preserve the append-only integration and attest its binding
                    # to the memory erased by this job.
                    counts["sleep_memories"] = len(scope.sleep_memory_links)
                    for link in scope.sleep_memory_links.values():
                        self._receipt(
                            connection,
                            result["job_id"],
                            "sleep_integrations",
                            link["integration_id"],
                            link["result_hash"],
                            content_hash(dict(link)),
                        )
                counts.update(
                    self._erase_affected_memories(connection, result["job_id"], subject_id, scope)
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
                self._verify(connection, subject_id, conversation_id, result["job_id"], scope)
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
        job_id: str,
        scope: _ErasureScope,
    ) -> None:
        state = _row(connection, subject, window)
        if state["title"] != ERASED or state["status"] != "deleted" or state["pinned"]:
            raise IntegrityError("private erasure window residual")
        for original in scope.messages:
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
        for original in scope.calls:
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
        ConversationErasure._verify_erased_memories(connection, subject, job_id, scope.memory_ids)
        sleep_ids = ConversationErasure._verify_reflection_receipts(
            connection, subject, window, job_id, len(scope.reflection_hashes)
        )
        ConversationErasure._verify_sleep_memory_receipts(
            connection, subject, job_id, sleep_ids, scope.memory_ids, len(scope.sleep_memory_links)
        )

    @staticmethod
    def _verify_reflection_receipts(
        connection: sqlite3.Connection, subject: str, window: str, job_id: str, expected: int
    ) -> set[str]:
        from noyra.sleep.integrity import SleepIntegrity

        # Purpose identifies the expected runs even if structural events were archived later.
        # Admission also requires an exact selected-call link for each affected run.
        calls = connection.execute(
            "SELECT DISTINCT c.call_id,c.purpose,"
            "(c.request_json IS NULL AND c.response_json IS ? AND c.response_hash IS ?) AS erased "
            "FROM private_model_sources s JOIN model_calls c ON c.call_id=s.call_id "
            "AND c.subject_id=s.subject_id WHERE s.subject_id=? AND s.conversation_id=? "
            "AND c.purpose GLOB 'sleep_reflection:*' LIMIT ?",
            (
                canonical_json(ERASED_MODEL),
                content_hash(ERASED_MODEL),
                subject,
                window,
                MAX_ERASURE_ROWS + 1,
            ),
        ).fetchall()
        receipts = {
            row[0]
            for row in connection.execute(
                "SELECT record_id FROM private_erasure_receipts WHERE job_id=? "
                "AND record_table='sleep_reflections' LIMIT ?",
                (job_id, MAX_ERASURE_ROWS + 1),
            )
        }
        if max(len(calls), len(receipts)) > MAX_ERASURE_ROWS:
            raise ErasureReviewRequired("private_erasure_scope_exceeds_limit")
        if len(receipts) != expected:
            raise IntegrityError("private erasure sleep reflection receipt counts do not match")
        if any(not call["erased"] for call in calls):
            raise IntegrityError("private erasure sleep model residual")
        sleep_ids = {row["purpose"].removeprefix("sleep_reflection:") for row in calls}
        rows = connection.execute(
            "SELECT r.* FROM json_each(?) ids JOIN sleep_runs s ON s.sleep_id=ids.value "
            "AND s.subject_id=? JOIN sleep_reflections r ON r.sleep_id=s.sleep_id",
            (canonical_json(sorted(sleep_ids)), subject),
        ).fetchall()
        if len(rows) != len(sleep_ids) or {row["reflection_id"] for row in rows} != receipts:
            raise IntegrityError("private erasure sleep reflection scope does not match receipts")
        for row in rows:
            SleepIntegrity._verify_erased_reflection(connection, row, subject, job_id)
        return sleep_ids

    @staticmethod
    def _verify_sleep_memory_receipts(
        connection: sqlite3.Connection,
        subject: str,
        job_id: str,
        sleep_ids: set[str],
        memory_ids: set[str],
        expected: int,
    ) -> None:
        links = _sleep_memory_links(connection, subject, sleep_ids)
        receipts = {
            row["record_id"]: (row["original_hash"], row["erased_hash"])
            for row in connection.execute(
                "SELECT record_id,original_hash,erased_hash FROM private_erasure_receipts "
                "WHERE job_id=? AND record_table='sleep_integrations' LIMIT ?",
                (job_id, MAX_ERASURE_ROWS + 1),
            )
        }
        link_ids = {link["target_id"] for link in links}
        if (
            len(links) != expected
            or len(receipts) != expected
            or len(link_ids) != expected
            or not link_ids <= memory_ids
        ):
            raise IntegrityError("private erasure sleep memory scope does not match receipts")
        for link in links:
            if receipts.get(link["integration_id"]) != (
                link["result_hash"],
                content_hash(dict(link)),
            ):
                raise IntegrityError("private erasure sleep memory evidence differs from receipt")
        if (
            link_ids
            and connection.execute(
                "SELECT 1 FROM memory_embedding_inputs i JOIN json_each(?) ids "
                "ON ids.value=i.memory_id LIMIT 1",
                (canonical_json(sorted(link_ids)),),
            ).fetchone()
        ):
            raise IntegrityError("private erasure sleep memory embedding input residual")

    @staticmethod
    def _verify_erased_memories(
        connection: sqlite3.Connection, subject: str, job_id: str, memory_ids: set[str]
    ) -> None:
        from noyra.mind.memory import MemoryStore

        if not memory_ids:
            return
        targets = canonical_json(sorted(memory_ids))
        digest = content_hash(ERASED)
        verified = 0
        historical_revisions = 0
        for memory in connection.execute(
            "SELECT m.* FROM json_each(?) ids "
            "JOIN memories m ON m.memory_id=ids.value AND m.subject_id=? "
            "JOIN private_erasure_receipts receipt ON receipt.job_id=? "
            "AND receipt.record_table='memories' AND receipt.record_id=m.memory_id "
            "AND receipt.erased_hash=m.state_hash",
            (targets, subject, job_id),
        ):
            current = MemoryStore._from_row(memory)
            if current.content != ERASED or current.status != "archived":
                raise IntegrityError("private erasure memory residual")
            revisions = 0
            for revision in connection.execute(
                "SELECT r.*,receipt.erased_hash FROM memory_revisions r "
                "LEFT JOIN private_erasure_receipts receipt ON receipt.job_id=? "
                "AND receipt.record_table='memory_revisions' AND receipt.record_id=r.revision_id "
                "WHERE r.memory_id=? ORDER BY r.revision_number",
                (job_id, current.memory_id),
            ):
                revisions += 1
                _memory_sources(revision["source_event_ids_json"])
                expected_state = MemoryStore._state_hash(
                    ERASED, revision["salience"], revision["confidence"], revision["status"]
                )
                is_current = revisions == current.current_revision
                historical_revisions += not is_current
                if historical_revisions > MAX_ERASURE_ROWS:
                    raise ErasureReviewRequired("private_erasure_scope_exceeds_limit")
                if (
                    revision["revision_number"] != revisions
                    or revision["content"] != ERASED
                    or revision["reason"] != "privacy erasure"
                    or revision["content_hash"] != digest
                    or revision["state_hash"] != expected_state
                    or revision["erased_hash"] != (None if is_current else expected_state)
                    or (is_current and expected_state != memory["state_hash"])
                ):
                    raise IntegrityError("private erasure memory revision residual")
            if revisions != current.current_revision:
                raise IntegrityError("private erasure memory history mismatch")
            verified += 1
        if verified != len(memory_ids):
            raise IntegrityError("private erasure memory scope mismatch")
        if connection.execute(
            "SELECT 1 FROM memory_embeddings e JOIN json_each(?) ids ON ids.value=e.memory_id "
            "UNION ALL SELECT 1 FROM memory_fts f JOIN json_each(?) ids ON ids.value=f.memory_id "
            "LIMIT 1",
            (targets, targets),
        ).fetchone():
            raise IntegrityError("private erasure index residual")

    def _verify_completed_receipts(
        self, subject: str, window: str, job: dict[str, Any]
    ) -> tuple[int, int]:
        counts = job["counts"]
        if not isinstance(counts, dict):
            raise IntegrityError("private erasure memory receipt counts are invalid")
        # Jobs predating memory reconstruction recorded only exclusive memories.
        exclusive = counts.get("exclusive_memories")
        reconstructed = counts.get("reconstructed_memories", 0)
        if (
            type(exclusive) is not int
            or exclusive < 0
            or type(reconstructed) is not int
            or reconstructed < 0
        ):
            raise IntegrityError("private erasure memory receipt counts are invalid")
        reflections = counts.get("sleep_reflections", 0)
        sleep_memories = counts.get("sleep_memories", 0)
        if type(reflections) is not int or reflections < 0:
            raise IntegrityError("private erasure sleep reflection counts are invalid")
        if type(sleep_memories) is not int or not 0 <= sleep_memories <= exclusive:
            raise IntegrityError("private erasure sleep memory counts are invalid")
        job_id = job["job_id"]
        with self.database.read_transaction() as connection:
            sleep_ids = self._verify_reflection_receipts(
                connection, subject, window, job_id, reflections
            )
            memory_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT record_id FROM private_erasure_receipts "
                    "WHERE job_id=? AND record_table='memories' LIMIT ?",
                    (job_id, MAX_ERASURE_ROWS + 1),
                )
            }
            if len(memory_ids) > MAX_ERASURE_ROWS:
                raise ErasureReviewRequired("private_erasure_scope_exceeds_limit")
            if len(memory_ids) != exclusive + reconstructed:
                raise IntegrityError("private erasure memory receipt counts do not match")
            self._verify_erased_memories(connection, subject, job_id, memory_ids)
            self._verify_sleep_memory_receipts(
                connection, subject, job_id, sleep_ids, memory_ids, sleep_memories
            )
        return reflections, sleep_memories

    @staticmethod
    def _receipt(connection: Any, job: str, table: str, key: str, old: str, new: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO private_erasure_receipts VALUES(?,?,?,?,?)",
            (job, table, key, old, new),
        )

    def _review(
        self, connection: Any, subject: str, window: str, *, deadline: float | None = None
    ) -> _ErasureScope:
        if deadline is None:
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
            reflection_hashes, reflection_events, sleep_memory_links = self._review_reflections(
                connection, subject, window, calls, events, deadline
            )
            for event in events | reflection_events:
                if connection.execute(
                    "SELECT 1 FROM events e,json_each(e.causal_parent_ids_json) parent "
                    "WHERE e.subject_id=? AND parent.value=? LIMIT 1",
                    (subject, event),
                ).fetchone():
                    raise ErasureReviewRequired("private_event_derivative_review_required")
            memory_source_events = events | reflection_events
            memory_ids = self._check_derivatives(
                connection,
                subject,
                events,
                reflection_events,
                bool(reflection_hashes),
                sleep_memory_links,
            )
            return _ErasureScope(
                messages,
                calls,
                events,
                memory_ids,
                reflection_hashes,
                sleep_memory_links,
                memory_source_events,
            )
        except sqlite3.OperationalError as error:
            if time.monotonic() > deadline:
                raise ErasureReviewRequired("private_erasure_review_deadline") from error
            raise
        finally:
            connection.set_progress_handler(None, 0)

    @staticmethod
    def _check_derivatives(
        connection: Any,
        subject: str,
        events: set[str],
        reflection_events: set[str],
        reviewed_sleep_checkpoints: bool,
        sleep_memory_links: dict[str, sqlite3.Row],
    ) -> set[str]:
        # Historical model calls did not record exact private input lineage. Require
        # review instead of guessing that an opaque reflection has no private text.
        if connection.execute(
            "SELECT 1 FROM model_calls WHERE subject_id=? AND "
            "NOT EXISTS(SELECT 1 FROM private_model_sources s "
            "WHERE s.call_id=model_calls.call_id) LIMIT 1",
            (subject,),
        ).fetchone():
            raise ErasureReviewRequired("legacy_private_provenance_review_required")
        for table in _UNREVIEWED_DOMAINS:
            if table == "state_snapshots" and reviewed_sleep_checkpoints:
                continue
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
            sources = _memory_sources(revision["source_event_ids_json"])
            sources_by_memory.setdefault(revision["memory_id"], set()).update(sources)
        # The bound reflection event is the durable source marker for its
        # selected model response. Taint downstream memory history by that
        # marker; the independent-revision gate may retain only pre-input state.
        tainted_events = events | reflection_events
        affected = {
            memory_id
            for memory_id, sources in sources_by_memory.items()
            if sources & tainted_events
        }
        if not sleep_memory_links.keys() <= affected:
            raise IntegrityError("sleep memory source history does not match the erasure window")
        for memory_id in sorted(affected):
            if not sources_by_memory[memory_id] <= tainted_events:
                if memory_id in sleep_memory_links:
                    raise ErasureReviewRequired("reflection_derivative_review_required")
                ConversationErasure._independent_memory_revision(connection, memory_id, events)
        if (
            affected
            and connection.execute(
                "SELECT 1 FROM sleep_integrations "
                "WHERE target_id IN (SELECT value FROM json_each(?)) "
                "AND integration_id NOT IN (SELECT value FROM json_each(?)) LIMIT 1",
                (
                    canonical_json(sorted(affected)),
                    canonical_json(
                        sorted(link["integration_id"] for link in sleep_memory_links.values())
                    ),
                ),
            ).fetchone()
        ):
            raise ErasureReviewRequired("reflection_derivative_review_required")
        return affected

    @staticmethod
    def _review_reflections(
        connection: sqlite3.Connection,
        subject: str,
        window: str,
        calls: list[sqlite3.Row],
        events: set[str],
        deadline: float,
    ) -> tuple[dict[str, str], set[str], dict[str, sqlite3.Row]]:
        from noyra.sleep.integrity import SleepIntegrity
        from noyra.sleep.types import SleepReflectionPlan

        review_required = "reflection_derivative_review_required"
        target_purposes = {
            row["purpose"] for row in calls if row["purpose"].startswith("sleep_reflection:")
        }
        affected: dict[str, str] = {}
        affected_events: set[str] = set()
        sleep_memory_links: dict[str, sqlite3.Row] = {}
        reviewed_purposes: set[str] = set()
        remaining_bytes = MAX_REFLECTION_BYTES
        for index, ref in enumerate(
            connection.execute(
                "SELECT r.reflection_id,r.sleep_id,r.event_id,r.plan_hash,"
                "s.status,s.reflection_event_id,p.snapshot_id,e.payload_hash,"
                "CASE WHEN e.payload_archive_key IS NULL AND e.event_type='sleep_reflection' "
                "AND e.privacy_level='private' AND e.source='subject' "
                "AND e.causal_parent_ids_json='[]' AND length(CAST(e.payload_json AS BLOB))<=1024 "
                "THEN e.payload_json END AS event_payload,"
                "length(CAST(r.plan_json AS BLOB))+length(CAST(r.summary AS BLOB))+"
                "length(CAST(r.facts_json AS BLOB))+length(CAST(r.contradictions_json AS BLOB))+"
                "length(CAST(r.prediction_errors_json AS BLOB))+"
                "length(CAST(r.unresolved_questions_json AS BLOB))+"
                "coalesce(length(CAST(r.public_diary_candidate AS BLOB)),0) AS content_bytes "
                "FROM sleep_reflections r JOIN sleep_runs s ON s.sleep_id=r.sleep_id "
                "LEFT JOIN events e ON e.event_id=r.event_id AND e.subject_id=s.subject_id "
                "LEFT JOIN state_snapshots p ON p.snapshot_id=s.checkpoint_id "
                "AND p.subject_id=s.subject_id WHERE s.subject_id=? LIMIT ?",
                (subject, MAX_ERASURE_ROWS + 1),
            ),
            start=1,
        ):
            remaining_bytes -= ref["content_bytes"]
            if index > MAX_ERASURE_ROWS or remaining_bytes < 0:
                raise ErasureReviewRequired("private_erasure_scope_exceeds_limit")
            if time.monotonic() > deadline:
                raise ErasureReviewRequired("private_erasure_review_deadline")
            if (
                ref["status"] != "complete"
                or ref["event_id"] != ref["reflection_event_id"]
                or ref["snapshot_id"] is None
                or ref["event_payload"] is None
            ):
                raise ErasureReviewRequired(review_required)
            payload = SleepIntegrity._json_value(
                ref["event_payload"], "sleep reflection event invalid"
            )
            if (
                not isinstance(payload, dict)
                or content_hash(payload) != ref["payload_hash"]
                or payload.get("sleep_id") != ref["sleep_id"]
                or not isinstance(payload.get("model_call_id"), str)
            ):
                raise ErasureReviewRequired(review_required)
            purpose = f"sleep_reflection:{ref['sleep_id']}"
            call = connection.execute(
                "SELECT c.call_id,min(s.conversation_id) AS first_window,"
                "max(s.conversation_id) AS last_window FROM model_calls c "
                "JOIN private_model_sources s ON s.call_id=c.call_id WHERE c.call_id=? "
                "AND c.subject_id=? AND c.purpose=? AND c.status='succeeded' "
                "GROUP BY c.call_id HAVING min(s.subject_id)=? AND max(s.subject_id)=?",
                (payload["model_call_id"], subject, purpose, subject, subject),
            ).fetchone()
            if call is None or call["first_window"] != call["last_window"]:
                raise ErasureReviewRequired(review_required)
            row = connection.execute(
                "SELECT * FROM sleep_reflections WHERE reflection_id=?", (ref["reflection_id"],)
            ).fetchone()
            SleepIntegrity._verify_reflection(connection, row, subject)
            is_target = call["first_window"] == window
            if row["plan_hash"] == ERASED_REFLECTION_HASH:
                if is_target:
                    raise ErasureReviewRequired(review_required)
                continue
            selected_call = connection.execute(
                "SELECT response_json,response_hash FROM model_calls WHERE call_id=? "
                "AND length(CAST(response_json AS BLOB))<=?",
                (call["call_id"], remaining_bytes),
            ).fetchone()
            if selected_call is None:
                raise ErasureReviewRequired(review_required)
            try:
                plan = SleepReflectionPlan.model_validate_json(row["plan_json"])
                raw_response = decompress_text(
                    selected_call["response_json"], max_bytes=remaining_bytes
                )
                if raw_response is None:
                    raise ErasureReviewRequired(review_required)
                remaining_bytes -= len(raw_response.encode("utf-8"))
                response = strict_json_loads(raw_response)
                if (
                    not isinstance(response, dict)
                    or content_hash(response) != selected_call["response_hash"]
                    or not isinstance(response.get("content"), str)
                ):
                    raise ErasureReviewRequired(review_required)
                selected_plan = SleepReflectionPlan.model_validate_json(response["content"])
            except PayloadLimitError as error:
                raise ErasureReviewRequired("private_erasure_scope_exceeds_limit") from error
            except (TypeError, ValueError) as error:
                raise ErasureReviewRequired(review_required) from error
            if (
                plan != selected_plan
                or canonical_json(plan.model_dump(mode="json")) != row["plan_json"]
                or payload
                != {
                    "sleep_id": row["sleep_id"],
                    "model_call_id": call["call_id"],
                    "summary_hash": content_hash(plan.summary),
                    "facts": len(plan.facts),
                    "contradictions": len(plan.contradictions),
                    "prediction_errors": len(plan.prediction_errors),
                    "unresolved_questions": len(plan.unresolved_questions),
                }
            ):
                raise ErasureReviewRequired(review_required)
            if not is_target:
                # An independent call must not retain explicit citations of
                # the target window in its persisted plan.
                if any(events.intersection(item.source_event_ids) for item in plan.memories):
                    raise ErasureReviewRequired(review_required)
                continue
            if (
                plan.belief_revisions
                or plan.goal_revisions
                or plan.retry_blocks
                or plan.personality_candidates
                or connection.execute(
                    "SELECT 1 FROM retry_blocks WHERE source_sleep_id=? "
                    "UNION ALL SELECT 1 FROM public_diary_entries WHERE source_sleep_id=? "
                    "UNION ALL SELECT 1 FROM autonomous_project_sleep_reflections WHERE sleep_id=? "
                    "LIMIT 1",
                    (row["sleep_id"],) * 3,
                ).fetchone()
            ):
                raise ErasureReviewRequired(review_required)
            links = _review_sleep_memories(
                connection, subject, row["sleep_id"], call["call_id"], plan.memories, events
            )
            if sleep_memory_links.keys() & links.keys():
                raise ErasureReviewRequired(review_required)
            sleep_memory_links.update(links)
            if len(sleep_memory_links) > MAX_ERASURE_ROWS:
                raise ErasureReviewRequired("private_erasure_scope_exceeds_limit")
            affected[row["reflection_id"]] = row["plan_hash"]
            affected_events.add(row["event_id"])
            reviewed_purposes.add(purpose)

        if reviewed_purposes != target_purposes:
            raise ErasureReviewRequired(review_required)
        if affected:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if version is None or int(version[0]) < 120:
                raise ErasureReviewRequired(review_required)
            ConversationErasure._review_sleep_checkpoints(connection, subject, deadline)
            if connection.execute(
                "SELECT 1 FROM audit_records a WHERE a.subject_id=? "
                "AND a.action='runtime_exported' AND NOT EXISTS(SELECT 1 FROM export_jobs j "
                "WHERE j.subject_id=a.subject_id AND j.export_kind='runtime' "
                "AND j.sha256=json_extract(a.payload_json,'$.archive_sha256')) LIMIT 1",
                (subject,),
            ).fetchone():
                raise ErasureReviewRequired("private_legacy_export_review_required")
        return affected, affected_events, sleep_memory_links

    @staticmethod
    def _review_sleep_checkpoints(
        connection: sqlite3.Connection, subject: str, deadline: float
    ) -> None:
        # SleepEngine writes an identifier-only checkpoint on entering deep sleep.
        # Any other snapshot shape still needs the broader derivative review.
        for index, snap in enumerate(
            connection.execute(
                "SELECT p.snapshot_id,p.reason,p.state_hash,s.sleep_id,s.reflection_event_id,"
                "CASE WHEN length(CAST(p.state_json AS BLOB))<=1024 THEN p.state_json END "
                "AS state_json "
                "FROM state_snapshots p LEFT JOIN sleep_runs s ON s.checkpoint_id=p.snapshot_id "
                "AND s.subject_id=p.subject_id AND s.status='complete' "
                "WHERE p.subject_id=? LIMIT ?",
                (subject, MAX_ERASURE_ROWS + 1),
            ),
            start=1,
        ):
            if index > MAX_ERASURE_ROWS:
                raise ErasureReviewRequired("private_erasure_scope_exceeds_limit")
            if time.monotonic() > deadline:
                raise ErasureReviewRequired("private_erasure_review_deadline")
            expected = {
                "kind": "sleep_checkpoint",
                "sleep_id": snap["sleep_id"],
                "reflection_event_id": snap["reflection_event_id"],
                "lifecycle": "deep_sleep",
            }
            if (
                snap["sleep_id"] is None
                or snap["reflection_event_id"] is None
                or snap["reason"] != "sleep integration checkpoint"
                or snap["state_json"] != canonical_json(expected)
                or snap["state_hash"] != content_hash(expected)
            ):
                raise ErasureReviewRequired("private_memory_derivative_review_required")

    @staticmethod
    def _review_exports(connection: Any, subject: str) -> None:
        if connection.execute(
            "SELECT 1 FROM export_jobs WHERE subject_id=? AND status IN ('queued','running') "
            "LIMIT 1",
            (subject,),
        ).fetchone():
            raise ErasureReviewRequired("private_exports_in_flight")
        for table in ("workflow_export_publications", "workflow_increment_publications"):
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                and connection.execute(
                    "SELECT 1 FROM " + table + " WHERE subject_id=? LIMIT 1", (subject,)
                ).fetchone()
            ):
                # These preserved derivatives cannot be retired by the legacy
                # managed conversation export cleanup path.
                raise ErasureReviewRequired("private_legacy_export_review_required")
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
        self, connection: sqlite3.Connection, job_id: str, subject: str, scope: _ErasureScope
    ) -> dict[str, int]:
        from noyra.mind.memory import MemoryStore

        # A selected sleep-reflection event is a causal marker for the exact
        # model response bound to this private window, not an independent source.
        events = scope.memory_source_events
        erased = rebuilt = 0
        for memory in connection.execute(
            "SELECT m.* FROM json_each(?) ids "
            "JOIN memories m ON m.memory_id=ids.value AND m.subject_id=? ORDER BY m.memory_id",
            (canonical_json(sorted(scope.memory_ids)), subject),
        ).fetchall():
            memory_id = memory["memory_id"]
            revisions = connection.execute(
                "SELECT revision_id,source_event_ids_json,content_hash,salience,confidence,status "
                "FROM memory_revisions WHERE memory_id=? ORDER BY revision_number DESC",
                (memory_id,),
            ).fetchall()
            source_lists = [
                _memory_sources(revision["source_event_ids_json"]) for revision in revisions
            ]
            sources = {source for items in source_lists for source in items}
            # Prior revisions retain their original references. The tombstone
            # uses one known affected revision's bounded list, even if the
            # union across history exceeds the per-revision limit.
            tombstone_sources = next(
                (items for items in source_lists if events.intersection(items)), None
            )
            if tombstone_sources is None:
                raise IntegrityError("private erasure memory source is missing")
            if not sources <= events:
                independent = ConversationErasure._independent_memory_revision(
                    connection, memory["memory_id"], events
                )
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
            digest = content_hash(ERASED)
            for revision in revisions:
                state_hash = MemoryStore._state_hash(
                    ERASED, revision["salience"], revision["confidence"], revision["status"]
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
            MemoryStore._insert_revision(
                connection,
                memory["memory_id"],
                int(memory["current_revision"]) + 1,
                ERASED,
                digest,
                float(memory["salience"]),
                float(memory["confidence"]),
                "archived",
                "privacy erasure",
                tombstone_sources,
                utc_now(),
            )
            current_state = MemoryStore._state_hash(
                ERASED, memory["salience"], memory["confidence"], "archived"
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
                (ERASED, digest, current_state, utc_now(), memory["memory_id"]),
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


def _sleep_memory_links(
    connection: sqlite3.Connection, subject: str, sleep_ids: set[str]
) -> list[sqlite3.Row]:
    if not sleep_ids:
        return []
    rows = connection.execute(
        "SELECT i.integration_id,i.sleep_id,i.integration_type,i.target_id,i.operation,"
        "CASE WHEN length(CAST(i.source_ids_json AS BLOB))<=22000 THEN i.source_ids_json END "
        "AS sources_json,i.result_hash,i.created_at,s.reflection_event_id "
        "FROM json_each(?) ids JOIN sleep_runs s ON s.sleep_id=ids.value AND s.subject_id=? "
        "JOIN sleep_integrations i ON i.sleep_id=s.sleep_id LIMIT ?",
        (canonical_json(sorted(sleep_ids)), subject, MAX_ERASURE_ROWS + 1),
    ).fetchall()
    if len(rows) > MAX_ERASURE_ROWS:
        raise ErasureReviewRequired("private_erasure_scope_exceeds_limit")
    return rows


def _review_sleep_memories(
    connection: sqlite3.Connection,
    subject: str,
    sleep_id: str,
    call_id: str,
    memories: tuple[SleepMemory, ...],
    events: set[str],
) -> dict[str, sqlite3.Row]:
    from noyra.mind.memory import MemoryStore

    # A receipt must identify a distinct creation whose original revision still
    # matches the accepted plan. Later revisions are reviewed separately.
    review_required = "reflection_derivative_review_required"
    links = _sleep_memory_links(connection, subject, {sleep_id})
    if len(links) != len(memories):
        raise ErasureReviewRequired(review_required)
    planned: Counter[tuple[str, str, float, float, tuple[str, ...]]] = Counter(
        (
            item.memory_type,
            content_hash(item.content),
            item.salience,
            item.confidence,
            tuple(dict.fromkeys(item.source_event_ids)),
        )
        for item in memories
    )
    cited_events = {event for item in memories for event in item.source_event_ids}
    if not cited_events <= events:
        raise ErasureReviewRequired(review_required)
    if cited_events and connection.execute(
        "SELECT count(*) FROM json_each(?) ids JOIN events e ON e.event_id=ids.value "
        "AND e.subject_id=? AND e.payload_archive_key IS NULL AND e.privacy_level='private' "
        "JOIN private_model_sources s ON s.subject_id=e.subject_id AND s.call_id=? "
        "AND s.interaction_id=json_extract(e.payload_json,'$.interaction_id')",
        (canonical_json(sorted(cited_events)), subject, call_id),
    ).fetchone()[0] != len(cited_events):
        raise ErasureReviewRequired(review_required)
    matched: dict[str, sqlite3.Row] = {}
    for link in links:
        memory_id = link["target_id"]
        if (
            not isinstance(memory_id, str)
            or not memory_id
            or memory_id in matched
            or link["integration_type"] != "memory"
            or link["operation"] != "created"
        ):
            raise ErasureReviewRequired(review_required)
        original = connection.execute(
            "SELECT m.memory_id,m.subject_id,m.memory_type,m.privacy_level,m.created_at,"
            "r.created_at AS updated_at,r.revision_number AS current_revision,"
            "r.status,r.salience,r.confidence,r.content_hash,r.state_hash,r.reason,"
            "r.source_event_ids_json,r.content FROM memories m JOIN memory_revisions r "
            "ON r.memory_id=m.memory_id AND r.revision_number=1 WHERE m.memory_id=? "
            "AND m.subject_id=? AND length(CAST(r.content AS BLOB))<=800000 "
            "AND length(CAST(r.source_event_ids_json AS BLOB))<=10000",
            (memory_id, subject),
        ).fetchone()
        if (
            original is None
            or original["status"] != "active"
            or original["privacy_level"] != "private"
            or original["reason"] != "sleep consolidation"
            or original["created_at"] != original["updated_at"]
        ):
            raise ErasureReviewRequired(review_required)
        sources = _memory_sources(original["source_event_ids_json"])
        if link["sources_json"] != canonical_json([*sources, link["reflection_event_id"]]):
            raise ErasureReviewRequired(review_required)
        record = MemoryStore._from_row(original)
        key = (record.memory_type, record.content_hash, record.salience, record.confidence, sources)
        if planned[key] < 1 or link["result_hash"] != content_hash(record.__dict__):
            raise ErasureReviewRequired(review_required)
        planned[key] -= 1
        matched[memory_id] = link
    return matched


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
    erased_reflections = False
    erased_sleep_memories = False
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
        job = erasure._resume_recorded_intent(subject, window, expected_revision=row[0])
        if job["status"] != "completed":
            job = erasure.run(subject, window)
        if job["status"] != "completed":
            raise ErasureReviewRequired("backup_private_deletion_replay_requires_review")
        # Older backups can carry a completion flag from a weaker verifier.
        reflections, sleep_memories = erasure._verify_completed_receipts(subject, window, job)
        erased_reflections |= bool(reflections)
        erased_sleep_memories |= bool(sleep_memories)
    if erased_reflections:
        database.assert_schema_contract()
        if any(restored_root.glob("noyra.sqlite3.pre-migration-v*.bak")):
            raise IntegrityError("private erasure restore contains a pre-migration copy")
        if erased_sleep_memories:
            # An FTS deletion can leave old posting lists in index segments.
            # Rebuild from the surviving FTS rows in the isolated restore tree.
            with database.transaction() as connection:
                connection.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
        # The staging tree is private and has no live readers. Rebuild it before
        # publication so free pages do not retain a previous reflection or call.
        with database.connection() as connection:
            connection.execute("VACUUM")
            if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise IntegrityError("private erasure restore WAL could not be cleared")


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
