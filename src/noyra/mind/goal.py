from __future__ import annotations

from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_finite_float,
    strict_int,
    utc_now,
)

from .errors import MindStateConflictError
from .types import GoalCandidate, GoalRecord
from .validation import validate_causal_source_ids

GOAL_STATUSES = frozenset(
    {
        "proposed",
        "candidate",
        "active",
        "paused",
        "reconsidering",
        "achieved",
        "abandoned",
    }
)
ALLOWED_GOAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"candidate", "abandoned"}),
    "candidate": frozenset({"active", "abandoned"}),
    "active": frozenset({"paused", "reconsidering", "achieved", "abandoned"}),
    "paused": frozenset({"active", "reconsidering", "abandoned"}),
    "reconsidering": frozenset({"active", "paused", "abandoned"}),
    "achieved": frozenset(),
    "abandoned": frozenset(),
}


class GoalStore:
    def __init__(self, database: Database):
        self.database = database

    def create_candidate(
        self,
        subject_id: str,
        candidate: GoalCandidate,
        *,
        causal_source_ids: tuple[str, ...],
        reason: str,
    ) -> GoalRecord:
        with self.database.transaction() as connection:
            return self._create_candidate_connection(
                connection,
                subject_id,
                candidate,
                causal_source_ids=causal_source_ids,
                reason=reason,
            )

    def _create_candidate_connection(
        self,
        connection: Any,
        subject_id: str,
        candidate: GoalCandidate,
        *,
        causal_source_ids: tuple[str, ...],
        reason: str,
    ) -> GoalRecord:
        if not causal_source_ids or not reason.strip():
            raise ValueError("goal formation requires causal sources and a reason")
        sources = validate_causal_source_ids(connection, subject_id, causal_source_ids)
        goal_id = new_id("goal")
        status = "proposed" if candidate.origin == "human_proposal" else "candidate"
        now = utc_now()
        state_hash = self._state_hash(
            candidate.title,
            candidate.description,
            status,
            candidate.priority,
            candidate.commitment,
            0,
            0,
        )
        connection.execute(
            """INSERT INTO goals(
                goal_id, subject_id, title, description, origin, status,
                priority, commitment, progress, emotional_pressure, state_hash,
                current_revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 1, ?, ?)""",
            (
                goal_id,
                subject_id,
                candidate.title,
                candidate.description,
                candidate.origin,
                status,
                candidate.priority,
                candidate.commitment,
                state_hash,
                now,
                now,
            ),
        )
        self._insert_revision(
            connection,
            goal_id,
            1,
            candidate.title,
            candidate.description,
            status,
            candidate.priority,
            candidate.commitment,
            0,
            0,
            reason,
            sources,
            now,
        )
        return self._load_connection(connection, goal_id)

    def accept_human_proposal(
        self,
        goal_id: str,
        *,
        rationale: str,
        causal_source_ids: tuple[str, ...],
        actor: str = "subject",
    ) -> GoalRecord:
        if actor != "subject":
            raise PermissionError("only the subject can accept a human goal proposal")
        goal = self.get(goal_id)
        if goal.origin != "human_proposal" or goal.status != "proposed":
            raise MindStateConflictError("goal is not a pending human proposal")
        return self.revise(
            goal_id,
            status="candidate",
            priority=goal.priority,
            commitment=goal.commitment,
            progress=goal.progress,
            emotional_pressure=goal.emotional_pressure,
            reason=rationale,
            causal_source_ids=causal_source_ids,
            expected_revision=goal.current_revision,
        )

    def activate(
        self,
        goal_id: str,
        *,
        rationale: str,
        causal_source_ids: tuple[str, ...],
        actor: str = "subject",
    ) -> GoalRecord:
        if actor != "subject":
            raise PermissionError("only the subject can activate a goal")
        goal = self.get(goal_id)
        return self.revise(
            goal_id,
            status="active",
            priority=goal.priority,
            commitment=goal.commitment,
            progress=goal.progress,
            emotional_pressure=goal.emotional_pressure,
            reason=rationale,
            causal_source_ids=causal_source_ids,
            expected_revision=goal.current_revision,
        )

    def revise(
        self,
        goal_id: str,
        *,
        status: str,
        priority: float,
        commitment: float,
        progress: float,
        emotional_pressure: float,
        reason: str,
        causal_source_ids: tuple[str, ...],
        title: str | None = None,
        description: str | None = None,
        expected_revision: int | None = None,
    ) -> GoalRecord:
        with self.database.transaction() as connection:
            return self._revise_connection(
                connection,
                goal_id,
                status=status,
                priority=priority,
                commitment=commitment,
                progress=progress,
                emotional_pressure=emotional_pressure,
                reason=reason,
                causal_source_ids=causal_source_ids,
                title=title,
                description=description,
                expected_revision=expected_revision,
            )

    def _revise_connection(
        self,
        connection: Any,
        goal_id: str,
        *,
        status: str,
        priority: float,
        commitment: float,
        progress: float,
        emotional_pressure: float,
        reason: str,
        causal_source_ids: tuple[str, ...],
        title: str | None = None,
        description: str | None = None,
        expected_revision: int | None = None,
    ) -> GoalRecord:
        if status not in GOAL_STATUSES:
            raise ValueError(f"invalid goal status: {status}")
        if not all(0 <= value <= 1 for value in (priority, commitment, progress)):
            raise ValueError("goal priority, commitment and progress must be zero to one")
        if not -1 <= emotional_pressure <= 1:
            raise ValueError("goal emotional pressure must be negative one to one")
        if not reason.strip() or not causal_source_ids:
            raise ValueError("goal revision requires a reason and causal sources")
        row = self._get_row(connection, goal_id)
        if row["status"] in {"achieved", "abandoned"}:
            raise MindStateConflictError("terminal goals cannot be revised")
        current_revision = int(row["current_revision"])
        if expected_revision is not None and current_revision != expected_revision:
            raise MindStateConflictError("goal revision changed before update")
        if status != row["status"] and status not in ALLOWED_GOAL_TRANSITIONS[row["status"]]:
            raise MindStateConflictError(f"cannot transition goal {row['status']} -> {status}")
        resolved_title = title if title is not None else row["title"]
        resolved_description = description if description is not None else row["description"]
        if not resolved_title.strip() or not resolved_description.strip():
            raise ValueError("goal title and description cannot be blank")
        sources = validate_causal_source_ids(connection, row["subject_id"], causal_source_ids)
        revision = current_revision + 1
        now = utc_now()
        state_hash = self._state_hash(
            resolved_title,
            resolved_description,
            status,
            priority,
            commitment,
            progress,
            emotional_pressure,
        )
        self._insert_revision(
            connection,
            goal_id,
            revision,
            resolved_title,
            resolved_description,
            status,
            priority,
            commitment,
            progress,
            emotional_pressure,
            reason,
            sources,
            now,
        )
        connection.execute(
            """UPDATE goals SET title = ?, description = ?, status = ?, priority = ?,
                commitment = ?, progress = ?, emotional_pressure = ?, state_hash = ?,
                current_revision = ?, updated_at = ?
                WHERE goal_id = ?""",
            (
                resolved_title,
                resolved_description,
                status,
                priority,
                commitment,
                progress,
                emotional_pressure,
                state_hash,
                revision,
                now,
                goal_id,
            ),
        )
        return self._load_connection(connection, goal_id)

    def apply_emotional_pressure_connection(
        self,
        connection: Any,
        goal_id: str,
        delta: float,
        *,
        causal_source_ids: tuple[str, ...],
    ) -> GoalRecord:
        row = self._get_row(connection, goal_id)
        pressure = max(-1.0, min(1.0, float(row["emotional_pressure"]) + delta))
        status = row["status"]
        if status == "active" and pressure <= -0.7:
            status = "reconsidering"
        return self._revise_connection(
            connection,
            goal_id,
            status=status,
            priority=float(row["priority"]),
            commitment=float(row["commitment"]),
            progress=float(row["progress"]),
            emotional_pressure=pressure,
            reason="affect changed goal pressure",
            causal_source_ids=causal_source_ids,
            title=row["title"],
            description=row["description"],
            expected_revision=int(row["current_revision"]),
        )

    def get(self, goal_id: str) -> GoalRecord:
        with self.database.connection() as connection:
            return self._load_connection(connection, goal_id)

    def ranked(
        self, subject_id: str, *, statuses: tuple[str, ...] = ("active",)
    ) -> list[GoalRecord]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM goals WHERE subject_id = ? AND status IN ({placeholders})",
                (subject_id, *statuses),
            ).fetchall()
        return sorted(
            (self._from_row(row) for row in rows),
            key=lambda goal: (goal.selection_score, goal.updated_at, goal.goal_id),
            reverse=True,
        )

    def revisions(self, goal_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            self._get_row(connection, goal_id)
            rows = connection.execute(
                "SELECT * FROM goal_revisions WHERE goal_id = ? ORDER BY revision_number",
                (goal_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _insert_revision(
        connection: Any,
        goal_id: str,
        revision: int,
        title: str,
        description: str,
        status: str,
        priority: float,
        commitment: float,
        progress: float,
        pressure: float,
        reason: str,
        sources: tuple[str, ...],
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO goal_revisions(
                revision_id, goal_id, revision_number, title, description, status,
                priority, commitment, progress, emotional_pressure, state_hash, reason,
                causal_source_ids_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("grev"),
                goal_id,
                revision,
                title,
                description,
                status,
                priority,
                commitment,
                progress,
                pressure,
                GoalStore._state_hash(
                    title,
                    description,
                    status,
                    priority,
                    commitment,
                    progress,
                    pressure,
                ),
                reason,
                canonical_json(list(sources)),
                created_at,
            ),
        )

    @staticmethod
    def _get_row(connection: Any, goal_id: str) -> Any:
        row = connection.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"goal not found: {goal_id}")
        return row

    @classmethod
    def _load_connection(cls, connection: Any, goal_id: str) -> GoalRecord:
        return cls._from_row(cls._get_row(connection, goal_id))

    @staticmethod
    def _from_row(row: Any) -> GoalRecord:
        goal_id = row["goal_id"]
        try:
            priority = strict_finite_float(row["priority"])
            commitment = strict_finite_float(row["commitment"])
            progress = strict_finite_float(row["progress"])
            emotional_pressure = strict_finite_float(row["emotional_pressure"])
            current_revision = strict_int(row["current_revision"])
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"goal durable state is invalid: {goal_id}") from error
        if (
            not 0.0 <= priority <= 1.0
            or not 0.0 <= commitment <= 1.0
            or not 0.0 <= progress <= 1.0
            or not -1.0 <= emotional_pressure <= 1.0
            or current_revision < 1
        ):
            raise IntegrityError(f"goal durable state is invalid: {goal_id}")
        status = row["status"]
        if not isinstance(status, str) or status not in GOAL_STATUSES:
            raise IntegrityError(f"goal durable state is invalid: {goal_id}")
        expected_hash = GoalStore._state_hash(
            row["title"],
            row["description"],
            status,
            priority,
            commitment,
            progress,
            emotional_pressure,
        )
        if expected_hash != row["state_hash"]:
            raise IntegrityError(f"goal state hash mismatch: {goal_id}")
        return GoalRecord(
            goal_id=goal_id,
            subject_id=row["subject_id"],
            title=row["title"],
            description=row["description"],
            origin=row["origin"],
            status=status,
            priority=priority,
            commitment=commitment,
            progress=progress,
            emotional_pressure=emotional_pressure,
            current_revision=current_revision,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _state_hash(
        title: str,
        description: str,
        status: str,
        priority: float,
        commitment: float,
        progress: float,
        emotional_pressure: float,
    ) -> str:
        return content_hash(
            {
                "title": title,
                "description": description,
                "status": status,
                "priority": float(priority),
                "commitment": float(commitment),
                "progress": float(progress),
                "emotional_pressure": float(emotional_pressure),
            }
        )
