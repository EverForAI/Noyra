from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.events import EventStore
from noyra.core.identity import IdentityStore
from noyra.core.lifecycle import LifecycleManager
from noyra.core.snapshots import SnapshotStore
from noyra.core.types import canonical_json, content_hash, new_id, utc_now
from noyra.mind.belief import BeliefStore
from noyra.mind.goal import GoalStore
from noyra.mind.memory import MemoryStore
from noyra.mind.validation import validate_causal_source_ids, validate_event_ids

from .errors import SleepStateConflictError
from .fatigue import FatigueTracker
from .types import SleepReflectionPlan, SleepRunRecord

SLEEP_TRANSITIONS: dict[str, frozenset[str]] = {
    "winding_down": frozenset({"reflective_sleep", "failed"}),
    "reflective_sleep": frozenset({"deep_sleep", "failed"}),
    "deep_sleep": frozenset({"waking", "failed"}),
    "waking": frozenset({"complete", "failed"}),
    "complete": frozenset(),
    "failed": frozenset(),
}
TRIGGERS = frozenset(
    {
        "fatigue",
        "budget",
        "failures",
        "staleness",
        "goal_conflict",
        "subject_choice",
        "schedule",
        "emergency",
    }
)


class SleepEngine:
    """Atomic, restart-safe sleep protocol and reflection integration boundary."""

    def __init__(
        self,
        database: Database,
        subject_id: str,
        *,
        clock: Callable[[], str] = utc_now,
    ):
        self.database = database
        self.subject_id = subject_id
        self.clock = clock
        self.events = EventStore(database)
        self.lifecycle = LifecycleManager(database, self.events, subject_id)
        self.fatigue = FatigueTracker(database)
        self.memories = MemoryStore(database)
        self.beliefs = BeliefStore(database)
        self.goals = GoalStore(database)
        self.snapshots = SnapshotStore(database)
        self.identities = IdentityStore(database)

    def start(
        self,
        trigger_type: str,
        reason: str,
        *,
        wake_after: str | None = None,
        emergency: bool = False,
    ) -> SleepRunRecord:
        if trigger_type not in TRIGGERS:
            raise ValueError(f"invalid sleep trigger: {trigger_type}")
        if not reason.strip() or len(reason) > 10_000:
            raise ValueError("sleep trigger reason is invalid")
        normalized_wake = self._normalize_optional_future(wake_after)
        fatigue = self.fatigue.ensure(self.subject_id)
        if trigger_type == "fatigue" and fatigue.fatigue < 90:
            raise SleepStateConflictError("fatigue-triggered sleep requires fatigue >= 90")
        if trigger_type == "budget" and fatigue.resource_pressure < 1:
            raise SleepStateConflictError("budget-triggered sleep requires exhausted resources")
        if trigger_type == "emergency":
            emergency = True
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM sleep_runs WHERE subject_id = ? "
                "AND status NOT IN ('complete', 'failed')",
                (self.subject_id,),
            ).fetchone()
            if existing is not None:
                if existing["trigger_type"] != trigger_type or existing["trigger_reason"] != reason:
                    raise SleepStateConflictError("another sleep run is already active")
                return self._from_row(existing)
            self._require_quiescent_actions(connection)
            lifecycle = connection.execute(
                "SELECT state FROM runtime_state WHERE subject_id = ?", (self.subject_id,)
            ).fetchone()
            if lifecycle is None or lifecycle["state"] != "active":
                raise SleepStateConflictError("sleep can begin only from active lifecycle state")
            self.lifecycle._transition_connection(
                connection, "winding_down", reason, actor="subject"
            )
            sleep_id = new_id("sleep")
            now = self.clock()
            state_hash = self._state_hash(
                "winding_down",
                trigger_type,
                reason,
                emergency,
                fatigue.fatigue,
                normalized_wake,
                None,
                None,
                1,
                None,
            )
            connection.execute(
                """INSERT INTO sleep_runs(
                    sleep_id, subject_id, status, trigger_type, trigger_reason, emergency,
                    pre_sleep_fatigue, wake_after, reflection_event_id, checkpoint_id,
                    state_hash, version, started_at, updated_at, completed_at
                ) VALUES (?, ?, 'winding_down', ?, ?, ?, ?, ?, NULL, NULL, ?, 1, ?, ?, NULL)""",
                (
                    sleep_id,
                    self.subject_id,
                    trigger_type,
                    reason,
                    int(emergency),
                    fatigue.fatigue,
                    normalized_wake,
                    state_hash,
                    now,
                    now,
                ),
            )
            self._insert_transition(connection, sleep_id, None, "winding_down", reason, now)
            return self._load_connection(connection, sleep_id)

    def begin_reflection(
        self, sleep_id: str, reason: str = "begin reflective integration"
    ) -> SleepRunRecord:
        with self.database.transaction() as connection:
            row = self._get_row(connection, sleep_id)
            if row["status"] == "reflective_sleep":
                return self._from_row(row)
            if row["status"] != "winding_down":
                raise SleepStateConflictError("reflection can begin only after winding down")
            self._require_quiescent_actions(connection)
            self.lifecycle._transition_connection(
                connection, "reflective_sleep", reason, actor="subject"
            )
            self._transition_run(connection, row, "reflective_sleep", reason)
            return self._load_connection(connection, sleep_id)

    def commit_reflection(self, sleep_id: str, plan: SleepReflectionPlan) -> SleepRunRecord:
        plan_payload = plan.model_dump(mode="json")
        plan_hash = content_hash(plan_payload)
        with self.database.transaction() as connection:
            row = self._get_row(connection, sleep_id)
            if row["status"] != "reflective_sleep":
                raise SleepStateConflictError("reflection can commit only in reflective sleep")
            existing = connection.execute(
                "SELECT * FROM sleep_reflections WHERE sleep_id = ?", (sleep_id,)
            ).fetchone()
            if existing is not None:
                if existing["plan_hash"] != plan_hash:
                    raise SleepStateConflictError("sleep already has a different reflection")
                return self._from_row(row)

            event = self.events._append_connection(
                connection,
                self.subject_id,
                "sleep_reflection",
                "subject",
                {
                    "sleep_id": sleep_id,
                    "summary_hash": content_hash(plan.summary),
                    "facts": len(plan.facts),
                    "contradictions": len(plan.contradictions),
                    "prediction_errors": len(plan.prediction_errors),
                    "unresolved_questions": len(plan.unresolved_questions),
                },
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=self.clock(),
                event_id=None,
            )
            reflection_id = new_id("sref")
            now = self.clock()
            connection.execute(
                """INSERT INTO sleep_reflections(
                    reflection_id, sleep_id, event_id, summary, facts_json,
                    contradictions_json, prediction_errors_json, unresolved_questions_json,
                    public_diary_candidate, plan_json, plan_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    reflection_id,
                    sleep_id,
                    event.event_id,
                    plan.summary,
                    canonical_json(list(plan.facts)),
                    canonical_json(list(plan.contradictions)),
                    canonical_json(list(plan.prediction_errors)),
                    canonical_json(list(plan.unresolved_questions)),
                    plan.public_diary_candidate,
                    canonical_json(plan_payload),
                    plan_hash,
                    now,
                ),
            )
            self._apply_memories(connection, sleep_id, event.event_id, plan, now)
            # Project managers may add richer sleep assessments; this local
            # snapshot is only a fallback and never calls a model.
            self._apply_goals(connection, sleep_id, event.event_id, plan, now)
            self._apply_beliefs(connection, sleep_id, event.event_id, plan, now)
            self._apply_retry_blocks(connection, sleep_id, plan, now)
            self._apply_personality_candidates(connection, sleep_id, plan, now)
            version = int(row["version"]) + 1
            state_hash = self._state_hash(
                row["status"],
                row["trigger_type"],
                row["trigger_reason"],
                bool(row["emergency"]),
                float(row["pre_sleep_fatigue"]),
                row["wake_after"],
                event.event_id,
                row["checkpoint_id"],
                version,
                row["completed_at"],
            )
            connection.execute(
                """UPDATE sleep_runs SET reflection_event_id = ?, state_hash = ?,
                    version = ?, updated_at = ? WHERE sleep_id = ?""",
                (event.event_id, state_hash, version, now, sleep_id),
            )
            return self._load_connection(connection, sleep_id)

    def enter_deep_sleep(
        self, sleep_id: str, reason: str = "reflection committed"
    ) -> SleepRunRecord:
        with self.database.transaction() as connection:
            row = self._get_row(connection, sleep_id)
            if row["status"] == "deep_sleep":
                return self._from_row(row)
            if row["status"] != "reflective_sleep" or row["reflection_event_id"] is None:
                raise SleepStateConflictError("deep sleep requires a committed reflection")
            identity = connection.execute(
                "SELECT state_version FROM subject_identity WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchone()
            if identity is None:
                raise NotFoundError(f"identity not found: {self.subject_id}")
            state_version = int(identity["state_version"]) + 1
            snapshot = self.snapshots._save_connection(
                connection,
                self.subject_id,
                {
                    "kind": "sleep_checkpoint",
                    "sleep_id": sleep_id,
                    "reflection_event_id": row["reflection_event_id"],
                    "lifecycle": "deep_sleep",
                },
                state_version=state_version,
                reason="sleep integration checkpoint",
                snapshot_id=None,
            )
            self.identities._update_checkpoint_connection(
                connection, self.subject_id, state_version, snapshot.snapshot_id, None
            )
            self.lifecycle._transition_connection(connection, "deep_sleep", reason, actor="subject")
            self._mark_fatigue_sleeping(connection, reason)
            self._transition_run(
                connection, row, "deep_sleep", reason, checkpoint_id=snapshot.snapshot_id
            )
            return self._load_connection(connection, sleep_id)

    def wake(
        self,
        sleep_id: str,
        reason: str,
        *,
        force: bool = False,
    ) -> SleepRunRecord:
        if not reason.strip():
            raise ValueError("wake reason is required")
        with self.database.transaction() as connection:
            row = self._get_row(connection, sleep_id)
            if row["status"] == "waking":
                return self._from_row(row)
            if row["status"] != "deep_sleep":
                raise SleepStateConflictError("wake can begin only from deep sleep")
            if (
                row["wake_after"]
                and not force
                and self._parse_time(self.clock()) < self._parse_time(row["wake_after"])
            ):
                raise SleepStateConflictError("scheduled wake time has not arrived")
            self.lifecycle._transition_connection(connection, "waking", reason, actor="subject")
            self._transition_run(connection, row, "waking", reason)
            return self._load_connection(connection, sleep_id)

    def complete_wake(self, sleep_id: str, reason: str = "wake checks completed") -> SleepRunRecord:
        with self.database.transaction() as connection:
            row = self._get_row(connection, sleep_id)
            if row["status"] == "complete":
                return self._from_row(row)
            if row["status"] != "waking":
                raise SleepStateConflictError("wake completion requires waking state")
            self.lifecycle._transition_connection(connection, "active", reason, actor="subject")
            hours = max(
                0.0,
                (
                    self._parse_time(self.clock()) - self._parse_time(row["started_at"])
                ).total_seconds()
                / 3600,
            )
            reset_resource_pressure = (
                row["trigger_type"] == "budget"
                and self._parse_time(self.clock()).date()
                > self._parse_time(row["started_at"]).date()
            )
            self._restore_fatigue_connection(
                connection,
                hours,
                reason,
                reset_resource_pressure=reset_resource_pressure,
            )
            self._transition_run(connection, row, "complete", reason, completed_at=self.clock())
            return self._load_connection(connection, sleep_id)

    def get(self, sleep_id: str) -> SleepRunRecord:
        with self.database.connection() as connection:
            return self._load_connection(connection, sleep_id)

    def current(self) -> SleepRunRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM sleep_runs WHERE subject_id = ? "
                "AND status NOT IN ('complete', 'failed')",
                (self.subject_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def release_retry_block(self, block_id: str, evidence_event_id: str) -> None:
        with self.database.transaction() as connection:
            validate_event_ids(connection, self.subject_id, (evidence_event_id,))
            row = connection.execute(
                "SELECT * FROM retry_blocks WHERE block_id = ? AND subject_id = ?",
                (block_id, self.subject_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"retry block not found: {block_id}")
            if row["status"] == "released":
                return
            evidence = connection.execute(
                "SELECT rowid FROM events WHERE event_id = ?", (evidence_event_id,)
            ).fetchone()
            if evidence is None or int(evidence["rowid"]) <= int(row["evidence_event_boundary"]):
                raise SleepStateConflictError(
                    "retry block release requires evidence recorded after the block"
                )
            now = self.clock()
            state_hash = self._retry_hash(
                row["goal_id"],
                row["strategy_id"],
                row["tool"],
                row["target"],
                row["reason"],
                "released",
                evidence_event_id,
                int(row["evidence_event_boundary"]),
            )
            connection.execute(
                """UPDATE retry_blocks SET status = 'released', release_evidence_event_id = ?,
                    state_hash = ?, released_at = ? WHERE block_id = ?""",
                (evidence_event_id, state_hash, now, block_id),
            )

    def _apply_memories(
        self,
        connection: Any,
        sleep_id: str,
        reflection_event_id: str,
        plan: SleepReflectionPlan,
        now: str,
    ) -> None:
        for item in plan.memories:
            sources = tuple(dict.fromkeys((*item.source_event_ids, reflection_event_id)))
            record = self.memories._create_connection(
                connection,
                self.subject_id,
                item.memory_type,
                item.content,
                salience=item.salience,
                confidence=item.confidence,
                source_event_ids=sources,
                privacy_level="private",
                reason="sleep consolidation",
            )
            self._integration(
                connection,
                sleep_id,
                "memory",
                record.memory_id,
                "created",
                sources,
                content_hash(record.__dict__),
                now,
            )

    def _apply_goals(
        self,
        connection: Any,
        sleep_id: str,
        reflection_event_id: str,
        plan: SleepReflectionPlan,
        now: str,
    ) -> None:
        seen: set[str] = set()
        for item in plan.goal_revisions:
            if item.goal_id in seen:
                raise SleepStateConflictError("a sleep plan cannot revise one goal twice")
            seen.add(item.goal_id)
            current = self.goals._load_connection(connection, item.goal_id)
            if current.subject_id != self.subject_id:
                raise SleepStateConflictError("sleep goal belongs to another subject")
            revised = self.goals._revise_connection(
                connection,
                item.goal_id,
                status=item.status,
                priority=item.priority,
                commitment=item.commitment,
                progress=item.progress,
                emotional_pressure=item.emotional_pressure,
                reason=item.reason,
                causal_source_ids=(reflection_event_id,),
                expected_revision=current.current_revision,
            )
            self._integration(
                connection,
                sleep_id,
                "goal",
                revised.goal_id,
                "revised",
                (reflection_event_id,),
                content_hash(revised.__dict__),
                now,
            )

    def _apply_beliefs(
        self,
        connection: Any,
        sleep_id: str,
        reflection_event_id: str,
        plan: SleepReflectionPlan,
        now: str,
    ) -> None:
        seen: set[str] = set()
        for item in plan.belief_revisions:
            if item.belief_id in seen:
                raise SleepStateConflictError("a sleep plan cannot revise one belief twice")
            seen.add(item.belief_id)
            current = self.beliefs._load_connection(connection, item.belief_id)
            if current.subject_id != self.subject_id:
                raise SleepStateConflictError("sleep belief belongs to another subject")
            supporting = tuple(dict.fromkeys((*item.supporting_event_ids, reflection_event_id)))
            revised = self.beliefs._revise_connection(
                connection,
                item.belief_id,
                proposition=item.proposition,
                confidence=item.confidence,
                status=item.status,
                supporting_event_ids=supporting,
                counter_event_ids=item.counter_event_ids,
                reason=item.reason,
                expected_revision=current.current_revision,
            )
            self._integration(
                connection,
                sleep_id,
                "belief",
                revised.belief_id,
                "revised",
                (*supporting, *item.counter_event_ids),
                content_hash(revised.__dict__),
                now,
            )

    def _apply_retry_blocks(
        self, connection: Any, sleep_id: str, plan: SleepReflectionPlan, now: str
    ) -> None:
        for item in plan.retry_blocks:
            action_ids = tuple(dict.fromkeys(item.action_ids))
            placeholders = ",".join("?" for _ in action_ids)
            rows = connection.execute(
                f"SELECT * FROM actions WHERE subject_id = ? AND action_id IN ({placeholders})",
                (self.subject_id, *action_ids),
            ).fetchall()
            if len(rows) != len(action_ids) or any(
                row["status"] not in {"failed", "unknown"}
                or row["tool"] != item.tool
                or row["target"] != item.target
                or row["goal_id"] != item.goal_id
                or row["strategy_id"] != item.strategy_id
                for row in rows
            ):
                raise SleepStateConflictError(
                    "retry blocks require two matching failed or unknown actions"
                )
            existing = connection.execute(
                """SELECT block_id FROM retry_blocks WHERE subject_id = ? AND status = 'active'
                   AND tool = ? AND target = ?
                   AND ((goal_id IS NULL AND ? IS NULL) OR goal_id = ?)
                   AND ((strategy_id IS NULL AND ? IS NULL) OR strategy_id = ?)""",
                (
                    self.subject_id,
                    item.tool,
                    item.target,
                    item.goal_id,
                    item.goal_id,
                    item.strategy_id,
                    item.strategy_id,
                ),
            ).fetchone()
            if existing is not None:
                continue
            block_id = new_id("rblk")
            event_boundary = int(
                connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0]
            )
            state_hash = self._retry_hash(
                item.goal_id,
                item.strategy_id,
                item.tool,
                item.target,
                item.reason,
                "active",
                None,
                event_boundary,
            )
            connection.execute(
                """INSERT INTO retry_blocks(
                    block_id, subject_id, goal_id, strategy_id, tool, target, reason,
                    source_sleep_id, status, release_evidence_event_id, state_hash,
                    evidence_event_boundary, created_at, released_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, ?, NULL)""",
                (
                    block_id,
                    self.subject_id,
                    item.goal_id,
                    item.strategy_id,
                    item.tool,
                    item.target,
                    item.reason,
                    sleep_id,
                    state_hash,
                    event_boundary,
                    now,
                ),
            )
            self._integration(
                connection,
                sleep_id,
                "retry_block",
                block_id,
                "blocked",
                action_ids,
                state_hash,
                now,
            )

    def _apply_personality_candidates(
        self, connection: Any, sleep_id: str, plan: SleepReflectionPlan, now: str
    ) -> None:
        for item in plan.personality_candidates:
            evidence = validate_causal_source_ids(connection, self.subject_id, item.evidence_ids)
            candidate_id = new_id("pcand")
            state_hash = content_hash(
                {
                    "trait": item.trait,
                    "direction": item.direction,
                    "confidence": item.confidence,
                    "evidence_ids": list(evidence),
                    "status": "candidate",
                }
            )
            connection.execute(
                """INSERT INTO personality_candidates(
                    candidate_id, subject_id, trait, direction, confidence,
                    evidence_ids_json, source_sleep_id, status, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)""",
                (
                    candidate_id,
                    self.subject_id,
                    item.trait,
                    item.direction,
                    item.confidence,
                    canonical_json(list(evidence)),
                    sleep_id,
                    state_hash,
                    now,
                ),
            )
            self._integration(
                connection,
                sleep_id,
                "personality_candidate",
                candidate_id,
                "created",
                evidence,
                state_hash,
                now,
            )

    @staticmethod
    def _integration(
        connection: Any,
        sleep_id: str,
        integration_type: str,
        target_id: str,
        operation: str,
        sources: tuple[str, ...],
        result_hash: str,
        now: str,
    ) -> None:
        connection.execute(
            """INSERT INTO sleep_integrations(
                integration_id, sleep_id, integration_type, target_id, operation,
                source_ids_json, result_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("sint"),
                sleep_id,
                integration_type,
                target_id,
                operation,
                canonical_json(list(dict.fromkeys(sources))),
                result_hash,
                now,
            ),
        )

    def _require_quiescent_actions(self, connection: Any) -> None:
        row = connection.execute(
            "SELECT action_id, status FROM actions WHERE subject_id = ? "
            "AND status IN ('prepared', 'executing') LIMIT 1",
            (self.subject_id,),
        ).fetchone()
        if row is not None:
            raise SleepStateConflictError(
                f"sleep requires action {row['action_id']} in {row['status']} to settle"
            )

    def _mark_fatigue_sleeping(self, connection: Any, reason: str) -> None:
        row = connection.execute(
            "SELECT * FROM fatigue_states WHERE subject_id = ?", (self.subject_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"fatigue state not found: {self.subject_id}")
        inputs = self._inputs_from_fatigue_row(row)
        now = self.clock()
        state_hash = FatigueTracker._state_hash(100, "sleeping", inputs)
        connection.execute(
            """UPDATE fatigue_states SET fatigue = 100, mode = 'sleeping', state_hash = ?,
                version = version + 1, updated_at = ? WHERE subject_id = ?""",
            (state_hash, now, self.subject_id),
        )
        connection.execute(
            """INSERT INTO fatigue_transitions(
                transition_id, subject_id, old_fatigue, new_fatigue, mode,
                resource_pressure, cognitive_load, frustration, goal_conflict,
                staleness, reason, state_hash, created_at
            ) VALUES (?, ?, ?, 100, 'sleeping', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("fat"),
                self.subject_id,
                float(row["fatigue"]),
                inputs.resource_pressure,
                inputs.cognitive_load,
                inputs.frustration,
                inputs.goal_conflict,
                inputs.staleness,
                reason,
                state_hash,
                now,
            ),
        )

    def _restore_fatigue_connection(
        self,
        connection: Any,
        hours: float,
        reason: str,
        *,
        reset_resource_pressure: bool = False,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM fatigue_states WHERE subject_id = ?", (self.subject_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"fatigue state not found: {self.subject_id}")
        old = float(row["fatigue"])
        fatigue = max(0.0, old - min(100.0, hours * 18.0))
        from .types import FatigueInputs

        inputs = FatigueInputs(
            resource_pressure=(0.0 if reset_resource_pressure else float(row["resource_pressure"])),
            cognitive_load=0.0,
            frustration=0.0,
            goal_conflict=0.0,
            staleness=0.0,
        )
        mode = FatigueTracker.mode_for(fatigue)
        now = self.clock()
        state_hash = FatigueTracker._state_hash(fatigue, mode, inputs)
        connection.execute(
            """UPDATE fatigue_states SET fatigue = ?, mode = ?, resource_pressure = ?,
                cognitive_load = 0, frustration = 0, goal_conflict = 0, staleness = 0,
                state_hash = ?, version = version + 1, updated_at = ? WHERE subject_id = ?""",
            (fatigue, mode, inputs.resource_pressure, state_hash, now, self.subject_id),
        )
        connection.execute(
            """INSERT INTO fatigue_transitions(
                transition_id, subject_id, old_fatigue, new_fatigue, mode,
                resource_pressure, cognitive_load, frustration, goal_conflict,
                staleness, reason, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?)""",
            (
                new_id("fat"),
                self.subject_id,
                old,
                fatigue,
                mode,
                inputs.resource_pressure,
                reason,
                state_hash,
                now,
            ),
        )

    @staticmethod
    def _inputs_from_fatigue_row(row: Any) -> Any:
        from .types import FatigueInputs

        return FatigueInputs(
            resource_pressure=float(row["resource_pressure"]),
            cognitive_load=float(row["cognitive_load"]),
            frustration=float(row["frustration"]),
            goal_conflict=float(row["goal_conflict"]),
            staleness=float(row["staleness"]),
        )

    def _transition_run(
        self,
        connection: Any,
        row: Any,
        to_status: str,
        reason: str,
        *,
        checkpoint_id: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        current = row["status"]
        if to_status not in SLEEP_TRANSITIONS[current]:
            raise SleepStateConflictError(f"cannot transition sleep {current} -> {to_status}")
        now = self.clock()
        version = int(row["version"]) + 1
        resolved_checkpoint = checkpoint_id or row["checkpoint_id"]
        resolved_completed = completed_at or row["completed_at"]
        state_hash = self._state_hash(
            to_status,
            row["trigger_type"],
            row["trigger_reason"],
            bool(row["emergency"]),
            float(row["pre_sleep_fatigue"]),
            row["wake_after"],
            row["reflection_event_id"],
            resolved_checkpoint,
            version,
            resolved_completed,
        )
        connection.execute(
            """UPDATE sleep_runs SET status = ?, checkpoint_id = ?, state_hash = ?,
                version = ?, updated_at = ?, completed_at = ? WHERE sleep_id = ?""",
            (
                to_status,
                resolved_checkpoint,
                state_hash,
                version,
                now,
                resolved_completed,
                row["sleep_id"],
            ),
        )
        self._insert_transition(connection, row["sleep_id"], current, to_status, reason, now)

    @staticmethod
    def _insert_transition(
        connection: Any,
        sleep_id: str,
        from_status: str | None,
        to_status: str,
        reason: str,
        created_at: str,
    ) -> None:
        state_hash = content_hash(
            {"from_status": from_status, "to_status": to_status, "reason": reason}
        )
        connection.execute(
            """INSERT INTO sleep_transitions(
                transition_id, sleep_id, from_status, to_status, reason, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (new_id("str"), sleep_id, from_status, to_status, reason, state_hash, created_at),
        )

    @staticmethod
    def _state_hash(
        status: str,
        trigger_type: str,
        trigger_reason: str,
        emergency: bool,
        pre_sleep_fatigue: float,
        wake_after: str | None,
        reflection_event_id: str | None,
        checkpoint_id: str | None,
        version: int,
        completed_at: str | None,
    ) -> str:
        return content_hash(
            {
                "status": status,
                "trigger_type": trigger_type,
                "trigger_reason": trigger_reason,
                "emergency": emergency,
                "pre_sleep_fatigue": float(pre_sleep_fatigue),
                "wake_after": wake_after,
                "reflection_event_id": reflection_event_id,
                "checkpoint_id": checkpoint_id,
                "version": version,
                "completed_at": completed_at,
            }
        )

    @staticmethod
    def _retry_hash(
        goal_id: str | None,
        strategy_id: str | None,
        tool: str,
        target: str,
        reason: str,
        status: str,
        release_event_id: str | None,
        event_boundary: int,
    ) -> str:
        return content_hash(
            {
                "goal_id": goal_id,
                "strategy_id": strategy_id,
                "tool": tool,
                "target": target,
                "reason": reason,
                "status": status,
                "release_evidence_event_id": release_event_id,
                "evidence_event_boundary": event_boundary,
            }
        )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("sleep time must be ISO-8601") from error
        if parsed.tzinfo is None:
            raise ValueError("sleep time must include a timezone")
        return parsed.astimezone(UTC)

    def _normalize_optional_future(self, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = self._parse_time(value)
        if parsed <= self._parse_time(self.clock()):
            raise ValueError("scheduled wake time must be in the future")
        return parsed.isoformat(timespec="milliseconds")

    @staticmethod
    def _get_row(connection: Any, sleep_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM sleep_runs WHERE sleep_id = ?", (sleep_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"sleep run not found: {sleep_id}")
        return row

    @classmethod
    def _load_connection(cls, connection: Any, sleep_id: str) -> SleepRunRecord:
        return cls._from_row(cls._get_row(connection, sleep_id))

    @classmethod
    def _from_row(cls, row: Any) -> SleepRunRecord:
        expected = cls._state_hash(
            row["status"],
            row["trigger_type"],
            row["trigger_reason"],
            bool(row["emergency"]),
            float(row["pre_sleep_fatigue"]),
            row["wake_after"],
            row["reflection_event_id"],
            row["checkpoint_id"],
            int(row["version"]),
            row["completed_at"],
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"sleep run hash mismatch: {row['sleep_id']}")
        return SleepRunRecord(
            row["sleep_id"],
            row["subject_id"],
            row["status"],
            row["trigger_type"],
            row["trigger_reason"],
            bool(row["emergency"]),
            float(row["pre_sleep_fatigue"]),
            row["wake_after"],
            row["reflection_event_id"],
            row["checkpoint_id"],
            int(row["version"]),
            row["started_at"],
            row["updated_at"],
            row["completed_at"],
        )
