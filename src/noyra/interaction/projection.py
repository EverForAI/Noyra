from __future__ import annotations

import base64
import binascii
import json
from typing import Any, ClassVar

from noyra.core.database import Database
from noyra.core.errors import NotFoundError
from noyra.core.redaction import redact_secrets
from noyra.core.types import canonical_json, strict_json_loads

from .diary import PublicDiaryStore
from .posts import PublicPostStore
from .store import InteractionStore


class PublicProjection:
    """Read-only views with an explicit anonymous/private state boundary.

    ``state`` is the anonymous ``PublicSubjectStateV1`` contract.  The former
    detailed state remains available through ``private_state`` for an
    authenticated read route; keeping the two methods separate prevents a
    caller from accidentally widening the public projection when adding a
    private field.
    """

    PUBLIC_STATE_SCHEMA = "PublicSubjectStateV1"
    PUBLIC_STATE_VERSION = 1
    PUBLIC_STATE_FIELDS = frozenset(
        {
            "schema",
            "schema_version",
            "subject_id",
            "display_name",
            "lifecycle",
            "online",
            "public_diary_count",
        }
    )
    PUBLIC_LIFECYCLE_FIELDS = frozenset({"state"})
    PUBLIC_ONLINE_LIFECYCLE_STATES = frozenset(
        {
            "booting",
            "orienting",
            "active",
            "paused",
            "winding_down",
            "reflective_sleep",
            "deep_sleep",
            "waking",
            "resetting",
        }
    )
    RUNTIME_LOG_CURSOR_VERSION = "runtime-log-cursor/v1"
    RUNTIME_LOG_SOURCES: ClassVar[tuple[tuple[str, str, str, str, str, str, str], ...]] = (
        (
            "event",
            "events",
            "occurred_at",
            "event_id",
            "processing_status",
            "event_type || ' / ' || source",
            "idx_runtime_logs_events_keyset",
        ),
        (
            "model_call",
            "model_calls",
            "created_at",
            "call_id",
            "status",
            "purpose || ' / ' || provider || ':' || model",
            "idx_runtime_logs_model_calls_keyset",
        ),
        (
            "action",
            "actions",
            "prepared_at",
            "action_id",
            "status",
            "action_type || ' / ' || tool",
            "idx_runtime_logs_actions_keyset",
        ),
        (
            "epistemic_review",
            "epistemic_review_runs",
            "created_at",
            "review_id",
            "status",
            "summary",
            "idx_runtime_logs_epistemic_keyset",
        ),
        (
            "relationship_social",
            "relationship_social_runs",
            "created_at",
            "social_id",
            "disposition",
            "topic || ' / ' || channel",
            "idx_runtime_logs_relationship_keyset",
        ),
        (
            "self_model",
            "self_models",
            "created_at",
            "self_model_id",
            "status",
            "'operational self-model version ' || version",
            "idx_runtime_logs_self_models_keyset",
        ),
        (
            "intrinsic_thought",
            "thought_episodes",
            "created_at",
            "thought_id",
            "disposition",
            "summary",
            "idx_runtime_logs_thoughts_keyset",
        ),
        (
            "metacognition",
            "metacognitive_decisions",
            "created_at",
            "decision_id",
            "strategy",
            "reason_code",
            "idx_runtime_logs_metacognition_keyset",
        ),
        (
            "motivation",
            "motivation_reviews",
            "created_at",
            "review_id",
            "status",
            "summary",
            "idx_runtime_logs_motivation_keyset",
        ),
        (
            "consciousness",
            "consciousness_frames",
            "created_at",
            "frame_id",
            "consciousness_state",
            "workflow || ' / ' || reason_code",
            "idx_runtime_logs_consciousness_keyset",
        ),
        (
            "routing",
            "cognitive_route_decisions",
            "created_at",
            "decision_id",
            "selected_route",
            "purpose || ' / ' || reason_code",
            "idx_runtime_logs_routing_keyset",
        ),
        (
            "autonomous_project",
            "autonomous_project_reviews",
            "created_at",
            "review_id",
            "disposition",
            "summary",
            "idx_runtime_logs_projects_keyset",
        ),
        (
            "audit",
            "audit_records",
            "occurred_at",
            "audit_id",
            "action",
            "actor",
            "idx_runtime_logs_audit_keyset",
        ),
    )

    def __init__(self, database: Database):
        self.database = database
        self.diary_store = PublicDiaryStore(database)
        self.interactions = InteractionStore(database)

    def state(self, subject_id: str) -> dict[str, Any]:
        """Return only fields approved for unauthenticated state reads.

        This method deliberately queries no goal, mission, project,
        metacognition, consciousness, fatigue, memory, relationship, or model
        tables.  The absence of those reads is part of the privacy boundary,
        rather than a response-time or frontend convention.
        """
        with self.database.connection() as connection:
            identity = connection.execute(
                """SELECT subject_id, project_name, personal_name FROM subject_identity
                   WHERE subject_id = ?""",
                (subject_id,),
            ).fetchone()
            lifecycle = connection.execute(
                "SELECT state FROM runtime_state WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            diary_count = connection.execute(
                "SELECT COUNT(*) FROM public_diary_entries WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()[0]
        if identity is None or lifecycle is None:
            raise NotFoundError(f"public subject state not found: {subject_id}")
        lifecycle_state = str(lifecycle["state"])
        state = {
            "schema": self.PUBLIC_STATE_SCHEMA,
            "schema_version": self.PUBLIC_STATE_VERSION,
            "subject_id": identity["subject_id"],
            "display_name": identity["personal_name"] or identity["project_name"],
            "lifecycle": {"state": lifecycle_state},
            "online": lifecycle_state in self.PUBLIC_ONLINE_LIFECYCLE_STATES,
            "public_diary_count": int(diary_count),
        }
        if (
            set(state) != self.PUBLIC_STATE_FIELDS
            or set(state["lifecycle"]) != self.PUBLIC_LIFECYCLE_FIELDS
        ):
            raise RuntimeError("PublicSubjectStateV1 serializer widened unexpectedly")
        return state

    def private_state(self, subject_id: str) -> dict[str, Any]:
        """Return the detailed state for an authenticated read-only route."""
        with self.database.connection() as connection:
            identity = connection.execute(
                """SELECT subject_id, project_name, personal_name, identity_status,
                          state_version, last_checkpoint FROM subject_identity
                   WHERE subject_id = ?""",
                (subject_id,),
            ).fetchone()
            lifecycle = connection.execute(
                "SELECT state, reason, version, changed_at FROM runtime_state WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            fatigue = connection.execute(
                "SELECT fatigue, mode, updated_at FROM fatigue_states WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            diary_count = connection.execute(
                "SELECT COUNT(*) FROM public_diary_entries WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()[0]
            goal_counts = connection.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
                          SUM(CASE WHEN status = 'candidate' THEN 1 ELSE 0 END) AS candidate
                   FROM goals WHERE subject_id = ?
                   AND status NOT IN ('achieved', 'abandoned')""",
                (subject_id,),
            ).fetchone()
            focus = connection.execute(
                """SELECT g.goal_id, g.title, g.status, r.created_at
                   FROM (
                       SELECT * FROM goal_governance_runs WHERE subject_id = ?
                       ORDER BY created_at DESC, governance_id DESC LIMIT 1
                   ) r
                   LEFT JOIN goals g ON g.goal_id = r.focus_goal_id""",
                (subject_id,),
            ).fetchone()
            evaluation_counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN outcome = 'progress' THEN 1 ELSE 0 END) AS progress, "
                "SUM(CASE WHEN outcome = 'failure' THEN 1 ELSE 0 END) AS failures "
                "FROM outcome_evaluations WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            epistemic_counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status = 'committed' THEN 1 ELSE 0 END) AS committed "
                "FROM epistemic_review_runs WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            memory_counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active, "
                "SUM(CASE WHEN status = 'archived' THEN 1 ELSE 0 END) AS archived "
                "FROM memories WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            recall_count = connection.execute(
                "SELECT COALESCE(SUM(access_count), 0) FROM memory_accesses WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()[0]
            social_counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN disposition = 'contact' THEN 1 ELSE 0 END) AS contacts, "
                "SUM(CASE WHEN disposition = 'request_help' THEN 1 ELSE 0 END) AS help_requests, "
                "SUM(CASE WHEN disposition IN ('wait', 'respect_distance') "
                "THEN 1 ELSE 0 END) AS non_contacts "
                "FROM relationship_social_runs WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            relationship_count = connection.execute(
                "SELECT COUNT(*) FROM relationships WHERE subject_id = ? AND entity_type = 'human'",
                (subject_id,),
            ).fetchone()[0]
            self_model = connection.execute(
                "SELECT version, status, created_at FROM self_models WHERE subject_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
            thought_counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN changed_state = 1 THEN 1 ELSE 0 END) AS changed, "
                "SUM(CASE WHEN created_goal_id IS NOT NULL THEN 1 ELSE 0 END) AS goals "
                "FROM thought_episodes WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            open_thought_count = connection.execute(
                "SELECT COUNT(*) FROM thought_agenda_items WHERE subject_id = ? "
                "AND status IN ('open', 'cooling')",
                (subject_id,),
            ).fetchone()[0]
            metacognitive_counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN strategy = 'wait' THEN 1 ELSE 0 END) AS waits, "
                "SUM(CASE WHEN strategy = 'sleep' THEN 1 ELSE 0 END) AS sleeps "
                "FROM metacognitive_decisions WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            latest_metacognition = connection.execute(
                "SELECT strategy, reason_code, score, created_at FROM metacognitive_decisions "
                "WHERE subject_id = ? ORDER BY created_at DESC, decision_id DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
            value_counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status = 'established' THEN 1 ELSE 0 END) AS established "
                "FROM value_profiles WHERE subject_id = ? AND status != 'retired'",
                (subject_id,),
            ).fetchone()
            current_mission = connection.execute(
                "SELECT title, horizon, commitment, confidence, status, updated_at "
                "FROM mission_candidates WHERE subject_id = ? AND status != 'retired' "
                "ORDER BY CASE status WHEN 'adopted' THEN 0 WHEN 'provisional' THEN 1 "
                "WHEN 'candidate' THEN 2 ELSE 3 END, commitment DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
            latest_frame = connection.execute(
                "SELECT sequence_number, consciousness_state, attention_type, workflow, "
                "reason_code, resource_pool, next_wake_at, created_at "
                "FROM consciousness_frames WHERE subject_id = ? "
                "ORDER BY sequence_number DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
            cognitive_pool_counts = connection.execute(
                "SELECT resource_pool, COUNT(*) AS calls, "
                "COALESCE(SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END), 0) AS succeeded "
                "FROM model_calls WHERE subject_id = ? GROUP BY resource_pool",
                (subject_id,),
            ).fetchall()
            project_counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active, "
                "SUM(CASE WHEN status IN ('paused','blocked') THEN 1 ELSE 0 END) AS waiting, "
                "SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed "
                "FROM autonomous_projects WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            current_project = connection.execute(
                "SELECT project_id, project_type, title, deliverable, status, progress, "
                "updated_at FROM autonomous_projects WHERE subject_id = ? "
                "AND status IN ('planned','active','paused','blocked') "
                "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'planned' THEN 1 "
                "WHEN 'blocked' THEN 2 ELSE 3 END, updated_at DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
        if identity is None or lifecycle is None:
            raise NotFoundError(f"public subject state not found: {subject_id}")
        return {
            "subject_id": identity["subject_id"],
            "project_name": identity["project_name"],
            "personal_name": identity["personal_name"],
            "identity_status": identity["identity_status"],
            "state_version": int(identity["state_version"]),
            "last_checkpoint": identity["last_checkpoint"],
            "lifecycle": {
                "state": lifecycle["state"],
                "reason": lifecycle["reason"],
                "version": int(lifecycle["version"]),
                "changed_at": lifecycle["changed_at"],
            },
            "fatigue": None
            if fatigue is None
            else {
                "score": float(fatigue["fatigue"]),
                "mode": fatigue["mode"],
                "updated_at": fatigue["updated_at"],
            },
            "public_diary_count": int(diary_count),
            "goal_summary": {
                "non_terminal_count": int(goal_counts["total"]),
                "active_count": int(goal_counts["active"] or 0),
                "candidate_count": int(goal_counts["candidate"] or 0),
                "focus": None
                if focus is None or focus["status"] != "active"
                else {
                    "goal_id": focus["goal_id"],
                    "title": focus["title"],
                    "chosen_at": focus["created_at"],
                },
            },
            "learning_summary": {
                "evaluation_count": int(evaluation_counts["total"]),
                "verified_progress_count": int(evaluation_counts["progress"] or 0),
                "failure_count": int(evaluation_counts["failures"] or 0),
                "epistemic_review_count": int(epistemic_counts["total"]),
                "epistemic_change_count": int(epistemic_counts["committed"] or 0),
                "memory_count": int(memory_counts["total"]),
                "active_memory_count": int(memory_counts["active"] or 0),
                "archived_memory_count": int(memory_counts["archived"] or 0),
                "memory_recall_count": int(recall_count),
            },
            "relationship_summary": {
                "known_human_count": int(relationship_count),
                "social_review_count": int(social_counts["total"]),
                "contact_count": int(social_counts["contacts"] or 0),
                "help_request_count": int(social_counts["help_requests"] or 0),
                "non_contact_count": int(social_counts["non_contacts"] or 0),
            },
            "self_model_summary": None
            if self_model is None
            else {
                "version": int(self_model["version"]),
                "status": self_model["status"],
                "updated_at": self_model["created_at"],
            },
            "thought_summary": {
                "open_agenda_count": int(open_thought_count),
                "episode_count": int(thought_counts["total"]),
                "changed_episode_count": int(thought_counts["changed"] or 0),
                "created_goal_count": int(thought_counts["goals"] or 0),
            },
            "metacognition_summary": {
                "decision_count": int(metacognitive_counts["total"]),
                "wait_count": int(metacognitive_counts["waits"] or 0),
                "sleep_choice_count": int(metacognitive_counts["sleeps"] or 0),
                "latest": None
                if latest_metacognition is None
                else {
                    "strategy": latest_metacognition["strategy"],
                    "reason_code": latest_metacognition["reason_code"],
                    "score": float(latest_metacognition["score"]),
                    "chosen_at": latest_metacognition["created_at"],
                },
            },
            "motivation_summary": {
                "value_count": int(value_counts["total"]),
                "established_value_count": int(value_counts["established"] or 0),
                "mission": None
                if current_mission is None
                else {
                    "title": current_mission["title"],
                    "horizon": current_mission["horizon"],
                    "commitment": float(current_mission["commitment"]),
                    "confidence": float(current_mission["confidence"]),
                    "status": current_mission["status"],
                    "updated_at": current_mission["updated_at"],
                },
            },
            "consciousness": None
            if latest_frame is None
            else {
                "sequence_number": int(latest_frame["sequence_number"]),
                "state": latest_frame["consciousness_state"],
                "attention_type": latest_frame["attention_type"],
                "workflow": latest_frame["workflow"],
                "reason_code": latest_frame["reason_code"],
                "resource_pool": latest_frame["resource_pool"],
                "next_wake_at": latest_frame["next_wake_at"],
                "created_at": latest_frame["created_at"],
            },
            "model_usage": {
                str(row["resource_pool"]): {
                    "calls": int(row["calls"]),
                    "succeeded": int(row["succeeded"]),
                }
                for row in cognitive_pool_counts
            },
            "project_summary": {
                "project_count": int(project_counts["total"]),
                "active_count": int(project_counts["active"] or 0),
                "waiting_count": int(project_counts["waiting"] or 0),
                "completed_count": int(project_counts["completed"] or 0),
                "current": None
                if current_project is None
                else {
                    "project_id": current_project["project_id"],
                    "project_type": current_project["project_type"],
                    "title": current_project["title"],
                    "deliverable": current_project["deliverable"],
                    "status": current_project["status"],
                    "progress": float(current_project["progress"]),
                    "updated_at": current_project["updated_at"],
                },
            },
        }

    def runtime_logs(
        self, subject_id: str, *, limit: int = 100, cursor: str | None = None
    ) -> dict[str, Any]:
        bounded = max(1, min(limit, 1_000))
        cursor_key: tuple[str, str, str] | None = None
        with self.database.read_transaction() as connection:
            if cursor is None:
                anchors = {
                    category: int(
                        connection.execute(
                            f'SELECT COALESCE(MAX(rowid), 0) FROM "{table}"'
                        ).fetchone()[0]
                    )
                    for category, table, *_ in self.RUNTIME_LOG_SOURCES
                }
            else:
                cursor_key, anchors = self._decode_runtime_log_cursor(cursor)
            merged: list[dict[str, Any]] = []
            for source in self.RUNTIME_LOG_SOURCES:
                category = source[0]
                query, parameters = self._runtime_log_query(
                    source,
                    subject_id,
                    anchors[category],
                    bounded + 1,
                    cursor_key,
                )
                merged.extend(dict(row) for row in connection.execute(query, parameters))
        merged.sort(
            key=lambda item: (
                str(item["occurred_at"]),
                str(item["category"]),
                str(item["record_id"]),
            ),
            reverse=True,
        )
        page = merged[:bounded]
        next_cursor = None
        if len(merged) > bounded and page:
            last = page[-1]
            next_cursor = self._encode_runtime_log_cursor(
                str(last["occurred_at"]),
                str(last["category"]),
                str(last["record_id"]),
                anchors,
            )
        safe_page = redact_secrets(page)
        if not isinstance(safe_page, list):
            raise TypeError("sanitized runtime log page is not a list")
        return {"items": safe_page, "next_cursor": next_cursor}

    @classmethod
    def _runtime_log_query(
        cls,
        source: tuple[str, str, str, str, str, str, str],
        subject_id: str,
        anchor: int,
        limit: int,
        cursor_key: tuple[str, str, str] | None,
    ) -> tuple[str, tuple[Any, ...]]:
        category, table, time_column, id_column, status, summary, index = source
        predicate = ""
        parameters: list[Any] = [category, subject_id, anchor]
        if cursor_key is not None:
            cursor_time, cursor_category, cursor_id = cursor_key
            if category < cursor_category:
                predicate = f' AND "{time_column}" <= ?'
                parameters.append(cursor_time)
            elif category == cursor_category:
                predicate = (
                    f' AND ("{time_column}" < ? OR ("{time_column}" = ? AND "{id_column}" < ?))'
                )
                parameters.extend((cursor_time, cursor_time, cursor_id))
            else:
                predicate = f' AND "{time_column}" < ?'
                parameters.append(cursor_time)
        parameters.append(limit)
        query = (
            f'SELECT "{time_column}" AS occurred_at, ? AS category, '
            f'"{id_column}" AS record_id, {status} AS status, {summary} AS summary '
            f'FROM "{table}" INDEXED BY "{index}" WHERE subject_id = ? AND rowid <= ?'
            f'{predicate} ORDER BY "{time_column}" DESC, "{id_column}" DESC LIMIT ?'
        )
        return query, tuple(parameters)

    @classmethod
    def _encode_runtime_log_cursor(
        cls,
        occurred_at: str,
        category: str,
        record_id: str,
        anchors: dict[str, int],
    ) -> str:
        payload = {
            "version": cls.RUNTIME_LOG_CURSOR_VERSION,
            "occurred_at": occurred_at,
            "category": category,
            "record_id": record_id,
            "anchors": anchors,
        }
        return (
            base64.urlsafe_b64encode(canonical_json(payload).encode("utf-8"))
            .rstrip(b"=")
            .decode("ascii")
        )

    @classmethod
    def _decode_runtime_log_cursor(cls, cursor: str) -> tuple[tuple[str, str, str], dict[str, int]]:
        if not cursor or len(cursor) > 8_192:
            raise ValueError("runtime log cursor is invalid")
        try:
            padding = "=" * (-len(cursor) % 4)
            payload = strict_json_loads(base64.urlsafe_b64decode(cursor + padding))
        except (binascii.Error, UnicodeError, ValueError) as error:
            raise ValueError("runtime log cursor is invalid") from error
        categories = {source[0] for source in cls.RUNTIME_LOG_SOURCES}
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "occurred_at", "category", "record_id", "anchors"}
            or payload["version"] != cls.RUNTIME_LOG_CURSOR_VERSION
            or not isinstance(payload["occurred_at"], str)
            or not isinstance(payload["category"], str)
            or payload["category"] not in categories
            or not isinstance(payload["record_id"], str)
            or not isinstance(payload["anchors"], dict)
            or set(payload["anchors"]) != categories
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > (1 << 63) - 1
                for value in payload["anchors"].values()
            )
        ):
            raise ValueError("runtime log cursor is invalid")
        anchors = {str(key): int(value) for key, value in payload["anchors"].items()}
        return (
            str(payload["occurred_at"]),
            str(payload["category"]),
            str(payload["record_id"]),
        ), anchors

    def diary(self, subject_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return [entry.__dict__ for entry in self.diary_store.list(subject_id, limit=limit)]

    def behavior_logs(self, subject_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT l.occurred_at AS original_occurred_at,
                          l.public_goal_reference AS original_public_goal_reference,
                          l.public_target AS original_public_target,
                          l.result_status AS original_result_status,
                          l.side_effect_summary AS original_side_effect_summary,
                          l.resource_summary AS original_resource_summary,
                          l.public_explanation AS original_public_explanation,
                          l.redaction_reason AS original_redaction_reason,
                          l.action_type, l.tool,
                          r.revision_number, r.occurred_at, r.public_goal_reference,
                          r.public_target, r.result_status, r.side_effect_summary,
                          r.resource_summary, r.public_explanation, r.redaction_reason
                   FROM behavior_logs l
                   JOIN behavior_log_revisions r ON r.log_id = l.log_id
                    AND r.revision_number = (
                        SELECT MAX(r2.revision_number)
                        FROM behavior_log_revisions r2 WHERE r2.log_id = l.log_id
                    )
                   WHERE l.subject_id = ?
                   ORDER BY r.occurred_at DESC, l.log_id DESC LIMIT ?""",
                (subject_id, bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def goals_view(self, subject_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            focus = connection.execute(
                "SELECT focus_goal_id FROM goal_governance_runs WHERE subject_id = ? "
                "ORDER BY created_at DESC, governance_id DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
            focus_goal_id = focus["focus_goal_id"] if focus is not None else None
            rows = connection.execute(
                """SELECT goal_id, title, description, origin, status, priority,
                          commitment, progress, created_at, updated_at
                   FROM goals WHERE subject_id = ?
                   ORDER BY CASE status
                       WHEN 'active' THEN 0 WHEN 'candidate' THEN 1
                       WHEN 'reconsidering' THEN 2 WHEN 'paused' THEN 3 ELSE 4 END,
                       priority DESC, updated_at DESC, goal_id DESC LIMIT ?""",
                (subject_id, bounded),
            ).fetchall()
        return [
            {
                **dict(row),
                "is_focus": row["goal_id"] == focus_goal_id and row["status"] == "active",
            }
            for row in rows
        ]

    def outcomes_view(self, subject_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT e.created_at, e.goal_id, g.title AS goal_title, e.strategy_kind,
                          e.outcome, e.progress_before, e.progress_after,
                          e.confidence_before, e.confidence_after, e.public_summary
                   FROM outcome_evaluations e JOIN goals g ON g.goal_id = e.goal_id
                   WHERE e.subject_id = ? ORDER BY e.created_at DESC, e.evaluation_id DESC
                   LIMIT ?""",
                (subject_id, bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def projects_view(self, subject_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT p.project_id, p.goal_id, g.title AS goal_title, p.project_type,
                          p.title, p.purpose, p.deliverable, p.acceptance_criteria_json,
                          p.size_class, p.estimated_duration_hours, p.status, p.progress,
                          p.current_phase_id, p.created_at, p.updated_at,
                          ph.title AS current_phase_title, ph.position AS current_phase_position,
                          (SELECT COUNT(*) FROM autonomous_project_phases all_ph
                           WHERE all_ph.project_id = p.project_id) AS phase_count,
                          (SELECT COUNT(*) FROM autonomous_project_assistance_requests ar
                           WHERE ar.project_id = p.project_id AND ar.status = 'open')
                           AS open_help_count,
                          (SELECT COUNT(*) FROM autonomous_project_executions ex
                           WHERE ex.project_id = p.project_id) AS execution_count,
                          (SELECT ex.status FROM autonomous_project_executions ex
                           WHERE ex.project_id = p.project_id
                           ORDER BY ex.created_at DESC, ex.execution_id DESC LIMIT 1)
                           AS latest_execution_status
                   FROM autonomous_projects p JOIN goals g ON g.goal_id = p.goal_id
                   LEFT JOIN autonomous_project_phases ph ON ph.phase_id = p.current_phase_id
                   WHERE p.subject_id = ?
                   ORDER BY CASE p.status WHEN 'active' THEN 0 WHEN 'planned' THEN 1
                       WHEN 'blocked' THEN 2 WHEN 'paused' THEN 3 ELSE 4 END,
                       p.updated_at DESC, p.project_id DESC LIMIT ?""",
                (subject_id, bounded),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["acceptance_criteria"] = json.loads(item.pop("acceptance_criteria_json"))
            result.append(item)
        return result

    def interactions_view(self, subject_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return [
            {
                "interaction_id": record.interaction_id,
                "direction": record.direction,
                "kind": record.kind,
                "channel": record.channel,
                "counterparty": record.counterparty,
                "content": record.content,
                "related_interaction_id": record.related_interaction_id,
                "status": record.status,
                "created_at": record.created_at,
                "decided_at": record.decided_at,
            }
            for record in self.interactions.list(subject_id, limit=limit)
            if record.direction == "outgoing" and record.channel.startswith("public:")
        ]

    def public_posts_view(self, subject_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return only moderated public posts; no author identity beyond its label."""
        return [
            {
                "post_id": post.post_id,
                "kind": post.kind,
                "title": post.title,
                "content": post.content,
                "author_label": post.author_label,
                "author_provenance": post.author_provenance,
                "published_at": post.published_at,
            }
            for post in PublicPostStore(self.database).public(subject_id, limit=limit)
        ]
