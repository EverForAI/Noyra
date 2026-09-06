from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_finite_float,
    strict_int,
    strict_json_loads,
    utc_now,
)

from .belief import BeliefStore
from .causal import ENTITY_TABLES, CausalStore
from .errors import CausalValidationError, MindStateConflictError
from .goal import GoalStore
from .memory import MemoryStore
from .relationship import RelationshipStore
from .types import (
    AffectImpulse,
    AffectRecord,
    AffectTransition,
    AppraisalInput,
    AppraisalRecord,
    ExperienceResult,
    GoalCandidate,
    GoalRecord,
    MoodRecord,
    PsychologicalSnapshot,
)


def _integrity_row(kind: str, identifier: object, reader: Callable[[Any], Any], row: Any) -> Any:
    try:
        return reader(row)
    except IntegrityError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrityError(f"{kind} durable state is invalid: {identifier}") from error


def _integrity_int(
    value: object,
    kind: str,
    identifier: object,
    *,
    minimum: int | None = None,
) -> int:
    try:
        parsed = strict_int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise IntegrityError(f"{kind} durable state is invalid: {identifier}") from error
    if minimum is not None and parsed < minimum:
        raise IntegrityError(f"{kind} durable state is invalid: {identifier}")
    return parsed


def _integrity_float(
    value: object,
    kind: str,
    identifier: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        parsed = strict_finite_float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise IntegrityError(f"{kind} durable state is invalid: {identifier}") from error
    if minimum is not None and parsed < minimum:
        raise IntegrityError(f"{kind} durable state is invalid: {identifier}")
    if maximum is not None and parsed > maximum:
        raise IntegrityError(f"{kind} durable state is invalid: {identifier}")
    return parsed


class MindEngine:
    """Atomic event -> appraisal -> affect -> goal -> psychology causal pipeline."""

    def __init__(self, database: Database, *, clock: Callable[[], str] = utc_now):
        self.database = database
        self.causal_store = CausalStore(database)
        self.goal_store = GoalStore(database)
        self._clock = clock

    def process_event(
        self,
        subject_id: str,
        event_id: str,
        appraisal: AppraisalInput,
        impulses: Sequence[AffectImpulse],
        goal_candidates: Sequence[GoalCandidate] = (),
        *,
        idempotency_key: str = "primary",
    ) -> ExperienceResult:
        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise ValueError("mind processing idempotency key must be 1-256 characters")
        if len(impulses) > 32 or len(goal_candidates) > 8:
            raise ValueError("mind processing proposal exceeds bounded causal changes")
        impulse_keys = {(item.emotion_type, item.target_type, item.target_id) for item in impulses}
        if len(impulse_keys) != len(impulses):
            raise ValueError("duplicate affect components in one appraisal are not allowed")
        input_hash = content_hash(
            {
                "appraisal": appraisal.model_dump(mode="json"),
                "impulses": [item.model_dump(mode="json") for item in impulses],
                "goal_candidates": [item.model_dump(mode="json") for item in goal_candidates],
            }
        )
        with self.database.transaction() as connection:
            self._validate_event(connection, subject_id, event_id)
            existing = connection.execute(
                "SELECT * FROM appraisals WHERE subject_id = ? AND event_id = ? "
                "AND processing_key = ?",
                (subject_id, event_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["input_hash"] != input_hash:
                    raise MindStateConflictError(
                        "mind processing key already has different causal input"
                    )
                return self._experience_from_appraisal(connection, existing)
            now = self._clock()
            appraisal_record = self._insert_appraisal(
                connection,
                subject_id,
                event_id,
                idempotency_key,
                input_hash,
                appraisal,
                now,
            )
            self.causal_store._add_connection(
                connection,
                subject_id,
                "event",
                event_id,
                "appraised_as",
                "appraisal",
                appraisal_record.appraisal_id,
                strength=1,
                metadata={},
            )
            transitions = self._decay_existing(
                connection, subject_id, appraisal_record.appraisal_id, now
            )
            impulse_transitions: list[AffectTransition] = []
            for impulse in impulses:
                transition = self._apply_impulse(
                    connection,
                    subject_id,
                    appraisal_record.appraisal_id,
                    impulse,
                    now,
                )
                impulse_transitions.append(transition)
                transitions.append(transition)

            goals: list[GoalRecord] = []
            for candidate in goal_candidates:
                motive_transition = self._motive_transition(candidate, impulse_transitions)
                if candidate.origin != "human_proposal" and (
                    motive_transition is None
                    or motive_transition.new_intensity < candidate.minimum_motive_intensity
                ):
                    raise CausalValidationError(
                        "autonomous goal candidate lacks current affective support"
                    )
                sources: tuple[str, ...] = (appraisal_record.appraisal_id,)
                if motive_transition is not None:
                    sources += (motive_transition.transition_id,)
                goal = self.goal_store._create_candidate_connection(
                    connection,
                    subject_id,
                    candidate,
                    causal_source_ids=sources,
                    reason="goal emerged from event appraisal and affect",
                )
                self.causal_store._add_connection(
                    connection,
                    subject_id,
                    "appraisal",
                    appraisal_record.appraisal_id,
                    "generated_candidate",
                    "goal",
                    goal.goal_id,
                    strength=max(0.1, candidate.commitment),
                    metadata={"origin": candidate.origin},
                )
                if motive_transition is not None:
                    self.causal_store._add_connection(
                        connection,
                        subject_id,
                        "affect_transition",
                        motive_transition.transition_id,
                        "motivated",
                        "goal",
                        goal.goal_id,
                        strength=motive_transition.new_intensity,
                        metadata={"emotion": motive_transition.emotion_type},
                    )
                goals.append(goal)

            for transition, impulse in zip(impulse_transitions, impulses, strict=True):
                self._apply_goal_pressure(
                    connection,
                    subject_id,
                    appraisal_record.appraisal_id,
                    transition,
                    impulse.target_type,
                    impulse.target_id,
                    impulse.goal_effect,
                    transition.new_intensity - transition.old_intensity,
                )

            mood = self._update_mood(connection, subject_id, now)
            snapshot = self._snapshot(
                connection, subject_id, appraisal_record.appraisal_id, mood, now
            )
            self.causal_store._add_connection(
                connection,
                subject_id,
                "appraisal",
                appraisal_record.appraisal_id,
                "committed_psychological_state",
                "psychological_snapshot",
                snapshot.snapshot_id,
                strength=1,
                metadata={"version": snapshot.version},
            )
            return ExperienceResult(
                appraisal=appraisal_record,
                transitions=tuple(transitions),
                goals=tuple(goals),
                mood=mood,
                snapshot=snapshot,
            )

    def current_affect(self, subject_id: str) -> list[AffectRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM affect_components WHERE subject_id = ? "
                "ORDER BY intensity DESC, emotion_type, target_key",
                (subject_id,),
            ).fetchall()
        return [self._affect_from_row(row) for row in rows]

    def current_mood(self, subject_id: str) -> MoodRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM mood_states WHERE subject_id = ?", (subject_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"mood not found for subject: {subject_id}")
        return self._mood_from_row(row)

    def latest_snapshot(self, subject_id: str) -> PsychologicalSnapshot:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM psychological_snapshots WHERE subject_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"psychological snapshot not found: {subject_id}")
        return self._snapshot_from_row(row)

    def verify_integrity(self, subject_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.database.read_transaction() as connection:
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise IntegrityError("mind state contains broken foreign keys")

            memory_count = 0
            memories = connection.execute(
                "SELECT m.*, r.revision_number AS verified_revision_number, "
                "r.state_hash AS verified_revision_state_hash, "
                "r.content AS verified_revision_content, "
                "r.content_hash AS verified_revision_content_hash, "
                "(SELECT MAX(latest.revision_number) FROM memory_revisions latest "
                "WHERE latest.memory_id = m.memory_id) AS latest_revision_number "
                "FROM memories m LEFT JOIN memory_revisions r ON r.memory_id = m.memory_id "
                "AND r.revision_number = m.current_revision WHERE m.subject_id = ? "
                "ORDER BY m.memory_id",
                (subject_id,),
            )
            for row in memories:
                memory_id = row["memory_id"]
                _integrity_row("memory", memory_id, MemoryStore._from_row, row)
                current_revision = _integrity_int(
                    row["current_revision"], "memory", memory_id, minimum=1
                )
                if (
                    row["verified_revision_number"] is None
                    or _integrity_int(
                        row["verified_revision_number"], "memory", memory_id, minimum=1
                    )
                    != current_revision
                    or _integrity_int(row["latest_revision_number"], "memory", memory_id, minimum=1)
                    != current_revision
                    or row["verified_revision_state_hash"] != row["state_hash"]
                    or content_hash(row["verified_revision_content"])
                    != row["verified_revision_content_hash"]
                ):
                    raise IntegrityError(f"memory revision mismatch: {row['memory_id']}")
                memory_count += 1
            counts["memories"] = memory_count

            belief_count = 0
            beliefs = connection.execute(
                "SELECT b.*, r.revision_number AS verified_revision_number, "
                "r.proposition AS verified_proposition, r.confidence AS verified_confidence, "
                "r.status AS verified_status, r.state_hash AS verified_revision_state_hash, "
                "r.proposition_hash AS verified_proposition_hash, "
                "(SELECT MAX(latest.revision_number) FROM belief_revisions latest "
                "WHERE latest.belief_id = b.belief_id) AS latest_revision_number "
                "FROM beliefs b LEFT JOIN belief_revisions r ON r.belief_id = b.belief_id "
                "AND r.revision_number = b.current_revision WHERE b.subject_id = ? "
                "ORDER BY b.belief_id",
                (subject_id,),
            )
            for row in beliefs:
                belief_id = row["belief_id"]
                _integrity_row("belief", belief_id, BeliefStore._from_row, row)
                current_revision = _integrity_int(
                    row["current_revision"], "belief", belief_id, minimum=1
                )
                if (
                    row["verified_revision_number"] is None
                    or _integrity_int(
                        row["verified_revision_number"], "belief", belief_id, minimum=1
                    )
                    != current_revision
                    or _integrity_int(row["latest_revision_number"], "belief", belief_id, minimum=1)
                    != current_revision
                ):
                    raise IntegrityError(f"belief revision mismatch: {row['belief_id']}")
                verified_confidence = _integrity_float(
                    row["verified_confidence"],
                    "belief",
                    belief_id,
                    minimum=0.0,
                    maximum=1.0,
                )
                expected = BeliefStore._revision_hash(
                    row["verified_proposition"],
                    verified_confidence,
                    row["verified_status"],
                )
                if (
                    expected != row["verified_revision_state_hash"]
                    or content_hash(row["verified_proposition"]) != row["verified_proposition_hash"]
                    or row["verified_proposition"] != row["proposition"]
                    or verified_confidence
                    != _integrity_float(
                        row["confidence"],
                        "belief",
                        belief_id,
                        minimum=0.0,
                        maximum=1.0,
                    )
                    or row["verified_status"] != row["status"]
                ):
                    raise IntegrityError(f"belief revision mismatch: {row['belief_id']}")
                belief_count += 1
            counts["beliefs"] = belief_count

            relationship_count = 0
            relationships = connection.execute(
                "SELECT rel.*, rev.revision_number AS verified_revision_number, "
                "rev.state_hash AS verified_revision_state_hash, "
                "(SELECT MAX(latest.revision_number) FROM relationship_revisions latest "
                "WHERE latest.relationship_id = rel.relationship_id) AS latest_revision_number "
                "FROM relationships rel LEFT JOIN relationship_revisions rev "
                "ON rev.relationship_id = rel.relationship_id "
                "AND rev.revision_number = rel.current_revision WHERE rel.subject_id = ? "
                "ORDER BY rel.relationship_id",
                (subject_id,),
            )
            for row in relationships:
                relationship_id = row["relationship_id"]
                _integrity_row("relationship", relationship_id, RelationshipStore._from_row, row)
                current_revision = _integrity_int(
                    row["current_revision"], "relationship", relationship_id, minimum=1
                )
                if (
                    row["verified_revision_number"] is None
                    or _integrity_int(
                        row["verified_revision_number"],
                        "relationship",
                        relationship_id,
                        minimum=1,
                    )
                    != current_revision
                    or _integrity_int(
                        row["latest_revision_number"],
                        "relationship",
                        relationship_id,
                        minimum=1,
                    )
                    != current_revision
                    or row["verified_revision_state_hash"] != row["state_hash"]
                ):
                    raise IntegrityError(
                        f"relationship revision mismatch: {row['relationship_id']}"
                    )
                relationship_count += 1
            counts["relationships"] = relationship_count

            goal_count = 0
            goals = connection.execute(
                "SELECT g.*, r.revision_number AS verified_revision_number, "
                "r.state_hash AS verified_revision_state_hash, "
                "(SELECT MAX(latest.revision_number) FROM goal_revisions latest "
                "WHERE latest.goal_id = g.goal_id) AS latest_revision_number "
                "FROM goals g LEFT JOIN goal_revisions r ON r.goal_id = g.goal_id "
                "AND r.revision_number = g.current_revision WHERE g.subject_id = ? "
                "ORDER BY g.goal_id",
                (subject_id,),
            )
            for row in goals:
                goal_id = row["goal_id"]
                _integrity_row("goal", goal_id, GoalStore._from_row, row)
                current_revision = _integrity_int(
                    row["current_revision"], "goal", goal_id, minimum=1
                )
                if (
                    row["verified_revision_number"] is None
                    or _integrity_int(row["verified_revision_number"], "goal", goal_id, minimum=1)
                    != current_revision
                    or _integrity_int(row["latest_revision_number"], "goal", goal_id, minimum=1)
                    != current_revision
                    or row["verified_revision_state_hash"] != row["state_hash"]
                ):
                    raise IntegrityError(f"goal revision mismatch: {row['goal_id']}")
                goal_count += 1
            counts["goals"] = goal_count

            appraisal_count = 0
            appraisals = connection.execute(
                "SELECT * FROM appraisals WHERE subject_id = ?", (subject_id,)
            )
            for row in appraisals:
                _integrity_row("appraisal", row["appraisal_id"], self._appraisal_from_row, row)
                appraisal_count += 1
            counts["appraisals"] = appraisal_count

            affect_count = 0
            affects = connection.execute(
                "SELECT * FROM affect_components WHERE subject_id = ?", (subject_id,)
            )
            for row in affects:
                affect_id = f"{row['emotion_type']}:{row['target_key']}"
                _integrity_row("affect", affect_id, self._affect_from_row, row)
                affect_count += 1
            counts["affect_components"] = affect_count

            mood_count = 0
            moods = connection.execute(
                "SELECT * FROM mood_states WHERE subject_id = ?", (subject_id,)
            )
            for row in moods:
                _integrity_row("mood", row["subject_id"], self._mood_from_row, row)
                mood_count += 1
            counts["mood_states"] = mood_count

            transition_count = 0
            transitions = connection.execute(
                "SELECT * FROM affect_transitions WHERE subject_id = ?", (subject_id,)
            )
            for row in transitions:
                _integrity_row(
                    "affect transition", row["transition_id"], self._transition_from_row, row
                )
                transition_count += 1
            counts["affect_transitions"] = transition_count

            link_count = 0
            links = connection.execute(
                "SELECT * FROM causal_links WHERE subject_id = ?", (subject_id,)
            )
            for row in links:
                _integrity_row("causal link", row["link_id"], CausalStore._from_row, row)
                link_count += 1
            counts["causal_links"] = link_count

            snapshot_count = 0
            snapshots = connection.execute(
                "SELECT * FROM psychological_snapshots WHERE subject_id = ?", (subject_id,)
            )
            for row in snapshots:
                _integrity_row(
                    "psychological snapshot", row["snapshot_id"], self._snapshot_from_row, row
                )
                snapshot_count += 1
            counts["psychological_snapshots"] = snapshot_count
        return counts

    def _insert_appraisal(
        self,
        connection: Any,
        subject_id: str,
        event_id: str,
        processing_key: str,
        input_hash: str,
        appraisal: AppraisalInput,
        now: str,
    ) -> AppraisalRecord:
        appraisal_id = new_id("apr")
        narrative_hash = content_hash(appraisal.narrative)
        state_hash = self._appraisal_hash(appraisal)
        connection.execute(
            """INSERT INTO appraisals(
                appraisal_id, subject_id, event_id, processing_key, input_hash,
                novelty, goal_congruence,
                controllability, certainty, agency, narrative, narrative_hash,
                state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                appraisal_id,
                subject_id,
                event_id,
                processing_key,
                input_hash,
                appraisal.novelty,
                appraisal.goal_congruence,
                appraisal.controllability,
                appraisal.certainty,
                appraisal.agency,
                appraisal.narrative,
                narrative_hash,
                state_hash,
                now,
            ),
        )
        return AppraisalRecord(
            appraisal_id,
            subject_id,
            event_id,
            appraisal.novelty,
            appraisal.goal_congruence,
            appraisal.controllability,
            appraisal.certainty,
            appraisal.agency,
            appraisal.narrative,
            narrative_hash,
            now,
        )

    def _decay_existing(
        self, connection: Any, subject_id: str, appraisal_id: str, now: str
    ) -> list[AffectTransition]:
        rows = connection.execute(
            "SELECT * FROM affect_components WHERE subject_id = ?", (subject_id,)
        ).fetchall()
        transitions: list[AffectTransition] = []
        for row in rows:
            affect = self._affect_from_row(row)
            elapsed_hours = self._elapsed_hours(affect.updated_at, now)
            decayed = affect.intensity * ((1 - affect.decay_rate) ** elapsed_hours)
            decayed = max(0.0, min(1.0, decayed))
            if decayed < 0.0001:
                decayed = 0.0
            if abs(decayed - affect.intensity) < 0.0001:
                continue
            self._write_affect(
                connection,
                affect.subject_id,
                affect.emotion_type,
                affect.target_key,
                affect.target_type,
                affect.target_id,
                decayed,
                affect.valence,
                affect.arousal,
                affect.dominance,
                affect.decay_rate,
                affect.goal_effect,
                affect.version + 1,
                now,
            )
            transition = self._insert_transition(
                connection,
                subject_id,
                appraisal_id,
                affect.emotion_type,
                affect.target_key,
                affect.target_type,
                affect.target_id,
                affect.intensity,
                0,
                decayed,
                affect.valence,
                affect.arousal,
                affect.dominance,
                affect.goal_effect,
                now,
                relation="decayed_by_time",
            )
            transitions.append(transition)
            self._apply_goal_pressure(
                connection,
                subject_id,
                appraisal_id,
                transition,
                affect.target_type,
                affect.target_id,
                affect.goal_effect,
                decayed - affect.intensity,
            )
        return transitions

    def _apply_impulse(
        self,
        connection: Any,
        subject_id: str,
        appraisal_id: str,
        impulse: AffectImpulse,
        now: str,
    ) -> AffectTransition:
        target_key = self._target_key(impulse.target_type, impulse.target_id)
        if impulse.target_type in ENTITY_TABLES:
            if impulse.target_id is None:
                raise CausalValidationError("typed affect target requires a target ID")
            self.causal_store._validate_entity(
                connection, subject_id, impulse.target_type, impulse.target_id
            )
        row = connection.execute(
            "SELECT * FROM affect_components WHERE subject_id = ? AND emotion_type = ? "
            "AND target_key = ?",
            (subject_id, impulse.emotion_type, target_key),
        ).fetchone()
        current_affect = self._affect_from_row(row) if row is not None else None
        old_intensity = current_affect.intensity if current_affect is not None else 0.0
        old_goal_effect = (
            current_affect.goal_effect if current_affect is not None else impulse.goal_effect
        )
        if (
            row is not None
            and old_intensity > 0.0001
            and abs(old_goal_effect - impulse.goal_effect) > 1e-9
        ):
            raise CausalValidationError(
                "goal effect cannot change while an affect component remains active"
            )
        version = current_affect.version + 1 if current_affect is not None else 1
        new_intensity = max(0.0, min(1.0, old_intensity + impulse.impulse))
        self._write_affect(
            connection,
            subject_id,
            impulse.emotion_type,
            target_key,
            impulse.target_type,
            impulse.target_id,
            new_intensity,
            impulse.valence,
            impulse.arousal,
            impulse.dominance,
            impulse.decay_rate,
            impulse.goal_effect,
            version,
            now,
        )
        return self._insert_transition(
            connection,
            subject_id,
            appraisal_id,
            impulse.emotion_type,
            target_key,
            impulse.target_type,
            impulse.target_id,
            old_intensity,
            impulse.impulse,
            new_intensity,
            impulse.valence,
            impulse.arousal,
            impulse.dominance,
            impulse.goal_effect,
            now,
            relation="caused_affect",
        )

    def _insert_transition(
        self,
        connection: Any,
        subject_id: str,
        appraisal_id: str,
        emotion_type: str,
        target_key: str,
        target_type: str,
        target_id: str | None,
        old_intensity: float,
        impulse: float,
        new_intensity: float,
        valence: float,
        arousal: float,
        dominance: float,
        goal_effect: float,
        now: str,
        *,
        relation: str,
    ) -> AffectTransition:
        transition_id = new_id("afx")
        state_hash = self._transition_hash(
            appraisal_id,
            emotion_type,
            target_key,
            target_type,
            target_id,
            old_intensity,
            impulse,
            new_intensity,
            valence,
            arousal,
            dominance,
            goal_effect,
        )
        connection.execute(
            """INSERT INTO affect_transitions(
                transition_id, subject_id, appraisal_id, emotion_type, target_key,
                target_type, target_id, old_intensity, impulse, new_intensity,
                valence, arousal, dominance, goal_effect, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                transition_id,
                subject_id,
                appraisal_id,
                emotion_type,
                target_key,
                target_type,
                target_id,
                old_intensity,
                impulse,
                new_intensity,
                valence,
                arousal,
                dominance,
                goal_effect,
                state_hash,
                now,
            ),
        )
        self.causal_store._add_connection(
            connection,
            subject_id,
            "appraisal",
            appraisal_id,
            relation,
            "affect_transition",
            transition_id,
            strength=impulse if impulse else old_intensity - new_intensity,
            metadata={"emotion": emotion_type, "target": target_key},
        )
        return AffectTransition(
            transition_id,
            appraisal_id,
            emotion_type,
            target_key,
            old_intensity,
            impulse,
            new_intensity,
            goal_effect,
            now,
        )

    def _write_affect(
        self,
        connection: Any,
        subject_id: str,
        emotion_type: str,
        target_key: str,
        target_type: str,
        target_id: str | None,
        intensity: float,
        valence: float,
        arousal: float,
        dominance: float,
        decay_rate: float,
        goal_effect: float,
        version: int,
        now: str,
    ) -> None:
        state_hash = self._affect_hash(
            emotion_type,
            target_key,
            intensity,
            valence,
            arousal,
            dominance,
            decay_rate,
            goal_effect,
            version,
        )
        connection.execute(
            """INSERT INTO affect_components(
                subject_id, emotion_type, target_key, target_type, target_id,
                intensity, valence, arousal, dominance, decay_rate, goal_effect,
                state_hash, updated_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_id, emotion_type, target_key) DO UPDATE SET
                target_type = excluded.target_type, target_id = excluded.target_id,
                intensity = excluded.intensity, valence = excluded.valence,
                arousal = excluded.arousal, dominance = excluded.dominance,
                decay_rate = excluded.decay_rate, goal_effect = excluded.goal_effect,
                state_hash = excluded.state_hash,
                updated_at = excluded.updated_at, version = excluded.version""",
            (
                subject_id,
                emotion_type,
                target_key,
                target_type,
                target_id,
                intensity,
                valence,
                arousal,
                dominance,
                decay_rate,
                goal_effect,
                state_hash,
                now,
                version,
            ),
        )

    def _update_mood(self, connection: Any, subject_id: str, now: str) -> MoodRecord:
        rows = connection.execute(
            "SELECT * FROM affect_components WHERE subject_id = ?", (subject_id,)
        ).fetchall()
        affects = [self._affect_from_row(row) for row in rows]
        total_intensity = sum(affect.intensity for affect in affects)
        if total_intensity:
            immediate_valence = (
                sum(affect.valence * affect.intensity for affect in affects) / total_intensity
            )
            immediate_arousal = (
                sum(affect.arousal * affect.intensity for affect in affects) / total_intensity
            )
        else:
            immediate_valence = 0.0
            immediate_arousal = 0.0
        row = connection.execute(
            "SELECT * FROM mood_states WHERE subject_id = ?", (subject_id,)
        ).fetchone()
        if row is None:
            old_valence, old_arousal, old_stability, version = 0.0, 0.0, 0.5, 0
        else:
            old = self._mood_from_row(row)
            old_valence, old_arousal, old_stability, version = (
                old.valence,
                old.arousal,
                old.stability,
                old.version,
            )
        valence = max(-1.0, min(1.0, old_valence * 0.8 + immediate_valence * 0.2))
        arousal = max(0.0, min(1.0, old_arousal * 0.7 + immediate_arousal * 0.3))
        stability_signal = 1 - min(1.0, abs(immediate_valence - old_valence))
        stability = max(0.0, min(1.0, old_stability * 0.9 + stability_signal * 0.1))
        new_version = version + 1
        state_hash = self._mood_hash(valence, arousal, stability, new_version)
        connection.execute(
            """INSERT INTO mood_states(
                subject_id, valence, arousal, stability, state_hash, version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_id) DO UPDATE SET
                valence = excluded.valence, arousal = excluded.arousal,
                stability = excluded.stability, state_hash = excluded.state_hash,
                version = excluded.version, updated_at = excluded.updated_at""",
            (subject_id, valence, arousal, stability, state_hash, new_version, now),
        )
        return MoodRecord(subject_id, valence, arousal, stability, new_version, now)

    def _snapshot(
        self,
        connection: Any,
        subject_id: str,
        appraisal_id: str,
        mood: MoodRecord,
        now: str,
    ) -> PsychologicalSnapshot:
        version = (
            int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM psychological_snapshots "
                    "WHERE subject_id = ?",
                    (subject_id,),
                ).fetchone()[0]
            )
            + 1
        )
        affect_rows = connection.execute(
            "SELECT * FROM affect_components WHERE subject_id = ? "
            "ORDER BY emotion_type, target_key",
            (subject_id,),
        ).fetchall()
        goal_rows = connection.execute(
            "SELECT * FROM goals WHERE subject_id = ? "
            "AND status IN ('proposed', 'candidate', 'active', 'paused', 'reconsidering') "
            "ORDER BY goal_id",
            (subject_id,),
        ).fetchall()
        affects = [self._affect_from_row(row) for row in affect_rows]
        goals = [self.goal_store._from_row(row) for row in goal_rows]
        state: dict[str, Any] = {
            "mood": {
                "valence": mood.valence,
                "arousal": mood.arousal,
                "stability": mood.stability,
                "version": mood.version,
            },
            "affect": [
                {
                    "emotion_type": affect.emotion_type,
                    "target_key": affect.target_key,
                    "target_type": affect.target_type,
                    "target_id": affect.target_id,
                    "intensity": affect.intensity,
                    "valence": affect.valence,
                    "arousal": affect.arousal,
                    "dominance": affect.dominance,
                    "decay_rate": affect.decay_rate,
                    "goal_effect": affect.goal_effect,
                    "version": affect.version,
                }
                for affect in affects
            ],
            "goals": [
                {
                    "goal_id": goal.goal_id,
                    "title": goal.title,
                    "description": goal.description,
                    "origin": goal.origin,
                    "status": goal.status,
                    "priority": goal.priority,
                    "commitment": goal.commitment,
                    "progress": goal.progress,
                    "emotional_pressure": goal.emotional_pressure,
                    "revision": goal.current_revision,
                }
                for goal in goals
            ],
        }
        snapshot_id = new_id("psy")
        state_hash = content_hash(state)
        connection.execute(
            """INSERT INTO psychological_snapshots(
                snapshot_id, subject_id, appraisal_id, version,
                state_json, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                subject_id,
                appraisal_id,
                version,
                canonical_json(state),
                state_hash,
                now,
            ),
        )
        return PsychologicalSnapshot(
            snapshot_id, subject_id, appraisal_id, version, state, state_hash, now
        )

    def _experience_from_appraisal(self, connection: Any, appraisal_row: Any) -> ExperienceResult:
        appraisal = self._appraisal_from_row(appraisal_row)
        transition_rows = connection.execute(
            "SELECT * FROM affect_transitions WHERE appraisal_id = ? "
            "ORDER BY created_at, transition_id",
            (appraisal.appraisal_id,),
        ).fetchall()
        transitions = tuple(self._transition_from_row(row) for row in transition_rows)
        goal_rows = connection.execute(
            """SELECT goals.* FROM causal_links
               JOIN goals ON goals.goal_id = causal_links.target_id
               WHERE causal_links.subject_id = ?
                 AND causal_links.source_type = 'appraisal'
                 AND causal_links.source_id = ?
                 AND causal_links.relation = 'generated_candidate'
                 AND causal_links.target_type = 'goal'
               ORDER BY goals.goal_id""",
            (appraisal.subject_id, appraisal.appraisal_id),
        ).fetchall()
        goals = tuple(self.goal_store._from_row(row) for row in goal_rows)
        snapshot_row = connection.execute(
            "SELECT * FROM psychological_snapshots WHERE appraisal_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (appraisal.appraisal_id,),
        ).fetchone()
        if snapshot_row is None:
            raise IntegrityError(
                f"appraisal has no psychological snapshot: {appraisal.appraisal_id}"
            )
        snapshot = self._snapshot_from_row(snapshot_row)
        mood_data = snapshot.state.get("mood")
        if not isinstance(mood_data, dict):
            raise IntegrityError(f"snapshot mood is invalid: {snapshot.snapshot_id}")
        try:
            mood = MoodRecord(
                appraisal.subject_id,
                float(mood_data["valence"]),
                float(mood_data["arousal"]),
                float(mood_data["stability"]),
                int(mood_data["version"]),
                snapshot.created_at,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(f"snapshot mood is invalid: {snapshot.snapshot_id}") from error
        return ExperienceResult(appraisal, transitions, goals, mood, snapshot)

    def _apply_goal_pressure(
        self,
        connection: Any,
        subject_id: str,
        appraisal_id: str,
        transition: AffectTransition,
        target_type: str,
        target_id: str | None,
        goal_effect: float,
        intensity_delta: float,
    ) -> None:
        if target_type != "goal" or target_id is None:
            return
        delta = goal_effect * intensity_delta
        if abs(delta) <= 1e-9:
            return
        revised = self.goal_store.apply_emotional_pressure_connection(
            connection,
            target_id,
            delta,
            causal_source_ids=(appraisal_id, transition.transition_id),
        )
        self.causal_store._add_connection(
            connection,
            subject_id,
            "affect_transition",
            transition.transition_id,
            "changed_goal_pressure",
            "goal",
            revised.goal_id,
            strength=delta,
            metadata={"pressure": revised.emotional_pressure},
        )

    @staticmethod
    def _validate_event(connection: Any, subject_id: str, event_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM events WHERE subject_id = ? AND event_id = ?",
            (subject_id, event_id),
        ).fetchone()
        if row is None:
            raise CausalValidationError("event is missing or belongs to another subject")

    @staticmethod
    def _motive_transition(
        candidate: GoalCandidate, transitions: Sequence[AffectTransition]
    ) -> AffectTransition | None:
        target_key = MindEngine._target_key(
            candidate.motive_target_type, candidate.motive_target_id
        )
        emotion = " ".join(candidate.motive_emotion.strip().lower().split())
        matches = [
            transition
            for transition in transitions
            if transition.emotion_type == emotion and transition.target_key == target_key
        ]
        return matches[-1] if matches else None

    @staticmethod
    def _target_key(target_type: str, target_id: str | None) -> str:
        return content_hash({"target_type": target_type, "target_id": target_id})

    @staticmethod
    def _elapsed_hours(previous: str, current: str) -> float:
        elapsed = datetime.fromisoformat(current) - datetime.fromisoformat(previous)
        return max(0.0, elapsed.total_seconds() / 3600)

    @staticmethod
    def _affect_hash(
        emotion_type: str,
        target_key: str,
        intensity: float,
        valence: float,
        arousal: float,
        dominance: float,
        decay_rate: float,
        goal_effect: float,
        version: int,
    ) -> str:
        return content_hash(
            {
                "emotion_type": emotion_type,
                "target_key": target_key,
                "intensity": float(intensity),
                "valence": float(valence),
                "arousal": float(arousal),
                "dominance": float(dominance),
                "decay_rate": float(decay_rate),
                "goal_effect": float(goal_effect),
                "version": version,
            }
        )

    @classmethod
    def _affect_from_row(cls, row: Any) -> AffectRecord:
        identifier = f"{row['emotion_type']}:{row['target_key']}"
        intensity = _integrity_float(
            row["intensity"], "affect", identifier, minimum=0.0, maximum=1.0
        )
        valence = _integrity_float(row["valence"], "affect", identifier, minimum=-1.0, maximum=1.0)
        arousal = _integrity_float(row["arousal"], "affect", identifier, minimum=0.0, maximum=1.0)
        dominance = _integrity_float(
            row["dominance"], "affect", identifier, minimum=-1.0, maximum=1.0
        )
        decay_rate = _integrity_float(
            row["decay_rate"], "affect", identifier, minimum=0.0, maximum=1.0
        )
        goal_effect = _integrity_float(
            row["goal_effect"], "affect", identifier, minimum=-1.0, maximum=1.0
        )
        version = _integrity_int(row["version"], "affect", identifier, minimum=1)
        expected = cls._affect_hash(
            row["emotion_type"],
            row["target_key"],
            intensity,
            valence,
            arousal,
            dominance,
            decay_rate,
            goal_effect,
            version,
        )
        if expected != row["state_hash"]:
            raise IntegrityError(
                f"affect state hash mismatch: {row['emotion_type']}:{row['target_key']}"
            )
        return AffectRecord(
            subject_id=row["subject_id"],
            emotion_type=row["emotion_type"],
            target_key=row["target_key"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            intensity=intensity,
            valence=valence,
            arousal=arousal,
            dominance=dominance,
            decay_rate=decay_rate,
            goal_effect=goal_effect,
            version=version,
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _mood_hash(valence: float, arousal: float, stability: float, version: int) -> str:
        return content_hash(
            {
                "valence": float(valence),
                "arousal": float(arousal),
                "stability": float(stability),
                "version": version,
            }
        )

    @classmethod
    def _mood_from_row(cls, row: Any) -> MoodRecord:
        identifier = row["subject_id"]
        valence = _integrity_float(row["valence"], "mood", identifier, minimum=-1.0, maximum=1.0)
        arousal = _integrity_float(row["arousal"], "mood", identifier, minimum=0.0, maximum=1.0)
        stability = _integrity_float(row["stability"], "mood", identifier, minimum=0.0, maximum=1.0)
        version = _integrity_int(row["version"], "mood", identifier, minimum=0)
        expected = cls._mood_hash(valence, arousal, stability, version)
        if expected != row["state_hash"]:
            raise IntegrityError(f"mood state hash mismatch: {row['subject_id']}")
        return MoodRecord(
            row["subject_id"],
            valence,
            arousal,
            stability,
            version,
            row["updated_at"],
        )

    @staticmethod
    def _snapshot_from_row(row: Any) -> PsychologicalSnapshot:
        snapshot_id = row["snapshot_id"]
        raw_state = row["state_json"]
        if not isinstance(raw_state, str):
            raise IntegrityError(f"psychological snapshot state is invalid: {snapshot_id}")
        state = strict_json_loads(raw_state)
        if not isinstance(state, dict) or content_hash(state) != row["state_hash"]:
            raise IntegrityError(f"psychological snapshot hash mismatch: {snapshot_id}")
        return PsychologicalSnapshot(
            snapshot_id,
            row["subject_id"],
            row["appraisal_id"],
            _integrity_int(row["version"], "psychological snapshot", snapshot_id, minimum=1),
            state,
            row["state_hash"],
            row["created_at"],
        )

    @staticmethod
    def _appraisal_from_row(row: Any) -> AppraisalRecord:
        appraisal_id = row["appraisal_id"]
        if content_hash(row["narrative"]) != row["narrative_hash"]:
            raise IntegrityError(f"appraisal narrative hash mismatch: {appraisal_id}")
        novelty = _integrity_float(
            row["novelty"], "appraisal", appraisal_id, minimum=0.0, maximum=1.0
        )
        goal_congruence = _integrity_float(
            row["goal_congruence"],
            "appraisal",
            appraisal_id,
            minimum=-1.0,
            maximum=1.0,
        )
        controllability = _integrity_float(
            row["controllability"],
            "appraisal",
            appraisal_id,
            minimum=0.0,
            maximum=1.0,
        )
        certainty = _integrity_float(
            row["certainty"], "appraisal", appraisal_id, minimum=0.0, maximum=1.0
        )
        expected_state = MindEngine._appraisal_hash(
            AppraisalInput(
                novelty=novelty,
                goal_congruence=goal_congruence,
                controllability=controllability,
                certainty=certainty,
                agency=row["agency"],
                narrative=row["narrative"],
            )
        )
        if expected_state != row["state_hash"]:
            raise IntegrityError(f"appraisal state hash mismatch: {appraisal_id}")
        return AppraisalRecord(
            appraisal_id,
            row["subject_id"],
            row["event_id"],
            novelty,
            goal_congruence,
            controllability,
            certainty,
            row["agency"],
            row["narrative"],
            row["narrative_hash"],
            row["created_at"],
        )

    @staticmethod
    def _appraisal_hash(appraisal: AppraisalInput) -> str:
        return content_hash(appraisal.model_dump(mode="json"))

    @staticmethod
    def _transition_from_row(row: Any) -> AffectTransition:
        transition_id = row["transition_id"]
        old_intensity = _integrity_float(
            row["old_intensity"],
            "affect transition",
            transition_id,
            minimum=0.0,
            maximum=1.0,
        )
        impulse = _integrity_float(
            row["impulse"],
            "affect transition",
            transition_id,
            minimum=-1.0,
            maximum=1.0,
        )
        new_intensity = _integrity_float(
            row["new_intensity"],
            "affect transition",
            transition_id,
            minimum=0.0,
            maximum=1.0,
        )
        valence = _integrity_float(
            row["valence"],
            "affect transition",
            transition_id,
            minimum=-1.0,
            maximum=1.0,
        )
        arousal = _integrity_float(
            row["arousal"],
            "affect transition",
            transition_id,
            minimum=0.0,
            maximum=1.0,
        )
        dominance = _integrity_float(
            row["dominance"],
            "affect transition",
            transition_id,
            minimum=-1.0,
            maximum=1.0,
        )
        goal_effect = _integrity_float(
            row["goal_effect"],
            "affect transition",
            transition_id,
            minimum=-1.0,
            maximum=1.0,
        )
        expected = MindEngine._transition_hash(
            row["appraisal_id"],
            row["emotion_type"],
            row["target_key"],
            row["target_type"],
            row["target_id"],
            old_intensity,
            impulse,
            new_intensity,
            valence,
            arousal,
            dominance,
            goal_effect,
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"affect transition hash mismatch: {transition_id}")
        return AffectTransition(
            transition_id,
            row["appraisal_id"],
            row["emotion_type"],
            row["target_key"],
            old_intensity,
            impulse,
            new_intensity,
            goal_effect,
            row["created_at"],
        )

    @staticmethod
    def _transition_hash(
        appraisal_id: str,
        emotion_type: str,
        target_key: str,
        target_type: str,
        target_id: str | None,
        old_intensity: float,
        impulse: float,
        new_intensity: float,
        valence: float,
        arousal: float,
        dominance: float,
        goal_effect: float,
    ) -> str:
        return content_hash(
            {
                "appraisal_id": appraisal_id,
                "emotion_type": emotion_type,
                "target_key": target_key,
                "target_type": target_type,
                "target_id": target_id,
                "old_intensity": float(old_intensity),
                "impulse": float(impulse),
                "new_intensity": float(new_intensity),
                "valence": float(valence),
                "arousal": float(arousal),
                "dominance": float(dominance),
                "goal_effect": float(goal_effect),
            }
        )
