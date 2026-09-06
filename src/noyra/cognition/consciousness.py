from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_int,
    strict_json_loads,
    utc_now,
)

ConsciousnessState = Literal[
    "awake_quiet",
    "attending",
    "thinking_economy",
    "thinking_deep",
    "researching",
    "acting",
    "waiting",
    "sleeping",
    "degraded",
]
AttentionType = Literal[
    "agenda",
    "goal",
    "observation",
    "relationship",
    "interaction",
    "subject",
    "resource_pool",
    "none",
]


@dataclass(frozen=True)
class ConsciousnessFrameRecord:
    frame_id: str
    subject_id: str
    sequence_number: int
    previous_frame_hash: str | None
    consciousness_state: str
    attention_type: str
    attention_id: str | None
    workflow: str
    reason_code: str
    internal_changes: tuple[str, ...]
    world_changes: tuple[str, ...]
    unresolved_tensions: tuple[str, ...]
    candidate_workflows: tuple[str, ...]
    resource_pool: str | None
    routing_decision_id: str | None
    next_wake_at: str | None
    wake_condition: dict[str, Any]
    state_hash: str
    created_at: str


class ConsciousnessFrameStore:
    """Append-only, restart-safe record of meaningful attention transitions."""

    def __init__(self, database: Database, subject_id: str):
        self.database = database
        self.subject_id = subject_id

    def ensure_initial(self) -> ConsciousnessFrameRecord:
        latest = self.latest()
        if latest is not None:
            return latest
        return self.append(
            "awake_quiet",
            workflow="orient",
            reason_code="runtime_consciousness_initialized",
            attention_type="subject",
            attention_id=self.subject_id,
            candidate_workflows=("wait",),
            wake_condition={"kind": "event_or_due_work"},
        )

    def observe_result(self, result: str) -> ConsciousnessFrameRecord:
        state, workflow, reason = self._classify_result(result)
        latest = self.latest()
        cutoff = "" if latest is None else latest.created_at
        route = self._latest_route(cutoff)
        metacognition = self._latest_metacognition(cutoff)
        attention_type: AttentionType = "subject"
        attention_id: str | None = self.subject_id
        candidates: tuple[str, ...] = (workflow,)
        if metacognition is not None:
            attention_type = cast(AttentionType, str(metacognition["target_type"]))
            if attention_type not in {
                "agenda",
                "goal",
                "observation",
                "relationship",
                "interaction",
                "subject",
                "resource_pool",
                "none",
            }:
                attention_type = "subject"
            attention_id = metacognition["target_id"]
            candidates = tuple(dict.fromkeys((str(metacognition["strategy"]), workflow)))
        resource_pool = None if route is None else route["pool"]
        routing_decision_id = None if route is None else str(route["decision_id"])
        if resource_pool == "economy" and state not in {"researching", "acting"}:
            state = "thinking_economy"
        elif resource_pool == "deep" and state not in {"researching", "acting"}:
            state = "thinking_deep"
        internal, world = self._changes_since_latest()
        tensions = self._current_tensions()
        signature = (
            state,
            attention_type,
            attention_id,
            workflow,
            reason,
            resource_pool,
            routing_decision_id,
            internal,
            world,
            tensions,
        )
        if latest is not None:
            prior_signature = (
                latest.consciousness_state,
                latest.attention_type,
                latest.attention_id,
                latest.workflow,
                latest.reason_code,
                latest.resource_pool,
                latest.routing_decision_id,
                latest.internal_changes,
                latest.world_changes,
                latest.unresolved_tensions,
            )
            if signature == prior_signature:
                return latest
        return self.append(
            state,
            workflow=workflow,
            reason_code=reason,
            attention_type=attention_type,
            attention_id=attention_id,
            internal_changes=internal,
            world_changes=world,
            unresolved_tensions=tensions,
            candidate_workflows=candidates,
            resource_pool=resource_pool,
            routing_decision_id=routing_decision_id,
            wake_condition=self._wake_condition(state),
        )

    def append(
        self,
        state: ConsciousnessState,
        *,
        workflow: str,
        reason_code: str,
        attention_type: AttentionType = "none",
        attention_id: str | None = None,
        internal_changes: Sequence[str] = (),
        world_changes: Sequence[str] = (),
        unresolved_tensions: Sequence[str] = (),
        candidate_workflows: Sequence[str] = (),
        resource_pool: str | None = None,
        routing_decision_id: str | None = None,
        next_wake_at: str | None = None,
        wake_condition: dict[str, Any] | None = None,
    ) -> ConsciousnessFrameRecord:
        if not workflow.strip() or not reason_code.strip():
            raise ValueError("consciousness workflow and reason are required")
        if (attention_type == "none") != (attention_id is None):
            raise ValueError("consciousness attention type and id disagree")
        now = utc_now()
        with self.database.transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM consciousness_frames WHERE subject_id = ? "
                "ORDER BY sequence_number DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
            sequence = 1 if previous is None else int(previous["sequence_number"]) + 1
            previous_hash = None if previous is None else str(previous["state_hash"])
            payload = {
                "subject_id": self.subject_id,
                "sequence_number": sequence,
                "previous_frame_hash": previous_hash,
                "consciousness_state": state,
                "attention_type": attention_type,
                "attention_id": attention_id,
                "workflow": workflow,
                "reason_code": reason_code,
                "internal_changes": list(dict.fromkeys(internal_changes)),
                "world_changes": list(dict.fromkeys(world_changes)),
                "unresolved_tensions": list(dict.fromkeys(unresolved_tensions)),
                "candidate_workflows": list(dict.fromkeys(candidate_workflows)),
                "resource_pool": resource_pool,
                "routing_decision_id": routing_decision_id,
                "next_wake_at": next_wake_at,
                "wake_condition": wake_condition or {},
                "created_at": now,
            }
            frame_id = new_id("cframe")
            state_hash = content_hash(payload)
            connection.execute(
                """INSERT INTO consciousness_frames(
                    frame_id, subject_id, sequence_number, previous_frame_hash,
                    consciousness_state, attention_type, attention_id, workflow,
                    reason_code, internal_changes_json, world_changes_json,
                    unresolved_tensions_json, candidate_workflows_json, resource_pool,
                    routing_decision_id, next_wake_at, wake_condition_json, state_hash,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    frame_id,
                    self.subject_id,
                    sequence,
                    previous_hash,
                    state,
                    attention_type,
                    attention_id,
                    workflow,
                    reason_code,
                    canonical_json(payload["internal_changes"]),
                    canonical_json(payload["world_changes"]),
                    canonical_json(payload["unresolved_tensions"]),
                    canonical_json(payload["candidate_workflows"]),
                    resource_pool,
                    routing_decision_id,
                    next_wake_at,
                    canonical_json(payload["wake_condition"]),
                    state_hash,
                    now,
                ),
            )
        frame = self.latest()
        assert frame is not None
        return frame

    def latest(self) -> ConsciousnessFrameRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM consciousness_frames WHERE subject_id = ? "
                "ORDER BY sequence_number DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def verify_integrity(self) -> dict[str, int]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM consciousness_frames WHERE subject_id = ? ORDER BY sequence_number",
                (self.subject_id,),
            ).fetchall()
        previous_hash: str | None = None
        for expected_sequence, row in enumerate(rows, 1):
            frame = self._from_row(row)
            if frame.sequence_number != expected_sequence:
                raise IntegrityError("consciousness frame sequence is discontinuous")
            if frame.previous_frame_hash != previous_hash:
                raise IntegrityError("consciousness frame hash chain is discontinuous")
            previous_hash = frame.state_hash
        return {"consciousness_frames": len(rows)}

    def _latest_route(self, cutoff: str) -> Any | None:
        with self.database.connection() as connection:
            return connection.execute(
                "SELECT decision_id, pool FROM cognitive_route_decisions "
                "WHERE subject_id = ? AND created_at > ? "
                "ORDER BY created_at DESC, decision_id DESC LIMIT 1",
                (self.subject_id, cutoff),
            ).fetchone()

    def _latest_metacognition(self, cutoff: str) -> Any | None:
        with self.database.connection() as connection:
            return connection.execute(
                "SELECT strategy, target_type, target_id FROM metacognitive_decisions "
                "WHERE subject_id = ? AND created_at > ? "
                "ORDER BY created_at DESC, decision_id DESC LIMIT 1",
                (self.subject_id, cutoff),
            ).fetchone()

    def _changes_since_latest(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        latest = self.latest()
        cutoff = "" if latest is None else latest.created_at
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT event_type, source FROM events WHERE subject_id = ? AND occurred_at > ? "
                "ORDER BY occurred_at DESC LIMIT 12",
                (self.subject_id, cutoff),
            ).fetchall()
        internal = tuple(
            dict.fromkeys(str(row["event_type"]) for row in rows if row["source"] != "world")
        )
        world = tuple(
            dict.fromkeys(str(row["event_type"]) for row in rows if row["source"] == "world")
        )
        return internal, world

    def _current_tensions(self) -> tuple[str, ...]:
        with self.database.connection() as connection:
            waiting = connection.execute(
                "SELECT DISTINCT pool FROM waiting_cognitive_tasks "
                "WHERE subject_id = ? AND status = 'waiting' ORDER BY pool",
                (self.subject_id,),
            ).fetchall()
            failed = connection.execute(
                "SELECT COUNT(*) FROM outcome_evaluations WHERE subject_id = ? "
                "AND outcome = 'failure'",
                (self.subject_id,),
            ).fetchone()[0]
        tensions = [f"resource_pool_unavailable:{row['pool']}" for row in waiting]
        if int(failed) > 0:
            tensions.append("unresolved_verified_failure")
        return tuple(tensions)

    @staticmethod
    def _classify_result(result: str) -> tuple[ConsciousnessState, str, str]:
        normalized = result.strip() or "wait"
        if "sleep" in normalized:
            return "sleeping", "sleep", normalized
        if "research" in normalized or "world_" in normalized:
            return "researching", "research", normalized
        if "action" in normalized:
            return "acting", "action", normalized
        if any(term in normalized for term in ("failed", "exhausted", "unavailable")):
            return "degraded", "wait", normalized
        if "wait" in normalized or "inactive" in normalized or "unchanged" in normalized:
            return "awake_quiet", "wait", normalized
        return "attending", normalized.split(":", 1)[0], normalized

    @staticmethod
    def _wake_condition(state: str) -> dict[str, Any]:
        if state == "degraded":
            return {"kind": "resource_recovery_or_retry_due"}
        if state == "sleeping":
            return {"kind": "sleep_lifecycle"}
        if state in {"waiting", "awake_quiet"}:
            return {"kind": "event_or_due_work"}
        return {"kind": "workflow_completion"}

    @classmethod
    def _from_row(cls, row: Any) -> ConsciousnessFrameRecord:
        def strings(raw: object) -> tuple[str, ...]:
            if not isinstance(raw, str):
                raise IntegrityError("consciousness frame JSON is invalid")
            try:
                value = strict_json_loads(raw)
            except (TypeError, ValueError) as error:
                raise IntegrityError("consciousness frame JSON is invalid") from error
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise IntegrityError("consciousness frame string list is invalid")
            return tuple(value)

        raw_wake_condition = row["wake_condition_json"]
        if not isinstance(raw_wake_condition, str):
            raise IntegrityError("consciousness wake condition JSON is invalid")
        try:
            wake_condition = strict_json_loads(raw_wake_condition)
        except (TypeError, ValueError) as error:
            raise IntegrityError("consciousness wake condition JSON is invalid") from error
        if not isinstance(wake_condition, dict):
            raise IntegrityError("consciousness wake condition must be an object")
        raw_sequence = row["sequence_number"]
        if isinstance(raw_sequence, bool) or not isinstance(raw_sequence, (int, str)):
            raise IntegrityError("consciousness frame sequence is invalid")
        try:
            sequence_number = strict_int(raw_sequence)
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError("consciousness frame sequence is invalid") from error
        if sequence_number < 1:
            raise IntegrityError("consciousness frame sequence is invalid")
        internal = strings(row["internal_changes_json"])
        world = strings(row["world_changes_json"])
        tensions = strings(row["unresolved_tensions_json"])
        candidates = strings(row["candidate_workflows_json"])
        payload = {
            "subject_id": row["subject_id"],
            "sequence_number": sequence_number,
            "previous_frame_hash": row["previous_frame_hash"],
            "consciousness_state": row["consciousness_state"],
            "attention_type": row["attention_type"],
            "attention_id": row["attention_id"],
            "workflow": row["workflow"],
            "reason_code": row["reason_code"],
            "internal_changes": list(internal),
            "world_changes": list(world),
            "unresolved_tensions": list(tensions),
            "candidate_workflows": list(candidates),
            "resource_pool": row["resource_pool"],
            "routing_decision_id": row["routing_decision_id"],
            "next_wake_at": row["next_wake_at"],
            "wake_condition": wake_condition,
            "created_at": row["created_at"],
        }
        if content_hash(payload) != row["state_hash"]:
            raise IntegrityError(f"consciousness frame hash mismatch: {row['frame_id']}")
        return ConsciousnessFrameRecord(
            row["frame_id"],
            row["subject_id"],
            sequence_number,
            row["previous_frame_hash"],
            row["consciousness_state"],
            row["attention_type"],
            row["attention_id"],
            row["workflow"],
            row["reason_code"],
            internal,
            world,
            tensions,
            candidates,
            row["resource_pool"],
            row["routing_decision_id"],
            row["next_wake_at"],
            wake_condition,
            row["state_hash"],
            row["created_at"],
        )
