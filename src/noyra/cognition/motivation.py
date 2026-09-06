from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.payload_codec import decompress_text
from noyra.core.types import canonical_json, content_hash, new_id, utc_now
from noyra.model import ModelGateway, ModelMessage
from noyra.model.errors import (
    BudgetExhaustedError,
    ModelCallStateError,
    ProviderCallError,
    StructuredOutputError,
)

from ._integrity import (
    durable_boundary,
    durable_float,
    durable_int,
    durable_json,
    durable_string_list,
)
from .settings import CognitionSettings
from .types import (
    MissionDevelopmentProposal,
    MotivationDevelopmentProposal,
    ValueDevelopmentProposal,
)


class MotivationDevelopmentValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValueProfileRecord:
    value_id: str
    subject_id: str
    value_key: str
    title: str
    description: str
    weight: float
    confidence: float
    status: str
    current_revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MissionCandidateRecord:
    mission_id: str
    subject_id: str
    title: str
    statement: str
    horizon: str
    commitment: float
    confidence: float
    status: str
    current_revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MotivationReviewRecord:
    review_id: str
    subject_id: str
    model_call_id: str
    status: str
    summary: str
    changed_value_ids: tuple[str, ...]
    changed_mission_ids: tuple[str, ...]
    created_at: str


@dataclass(frozen=True)
class _MotivationContext:
    serialized: str
    source_state_hash: str
    event_ids: frozenset[str]
    memory_ids: frozenset[str]
    belief_ids: frozenset[str]
    goal_ids: frozenset[str]
    relationship_ids: frozenset[str]
    values: dict[str, ValueProfileRecord]
    missions: dict[str, MissionCandidateRecord]
    completed_sleep_count: int


class MotivationDevelopment:
    """Develop durable values and long-horizon mission candidates from lived evidence."""

    def __init__(
        self,
        database: Database,
        subject_id: str,
        gateway: ModelGateway,
        settings: CognitionSettings,
        *,
        clock: Callable[[], str] = utc_now,
    ):
        self.database = database
        self.subject_id = subject_id
        self.gateway = gateway
        self.settings = settings
        self.clock = clock
        self.events = EventStore(database)

    async def run_due(self) -> str | None:
        latest = self.latest_review()
        context = self._context()
        if context is None or not self._is_due(latest, context.source_state_hash):
            return None
        if self._calls_today() >= self.settings.max_motivation_model_calls_per_day:
            return None
        round_number = self._review_count()
        purpose = f"motivation_development:{round_number}:{context.source_state_hash[:16]}"
        idempotency_key = f"motivation-development:{round_number}:{context.source_state_hash[:16]}"
        existing = self._existing(idempotency_key)
        if existing is not None:
            return f"motivation_development_{existing.status}"
        recovered = self._successful_proposal(purpose, context)
        if recovered is None:
            if self._terminal_call_exists(idempotency_key):
                return None
            try:
                result = await self.gateway.complete_structured(
                    self.subject_id,
                    purpose,
                    self._messages(context),
                    MotivationDevelopmentProposal,
                    idempotency_key=idempotency_key,
                    max_output_tokens=min(4_000, self.settings.max_output_tokens),
                    temperature=self.settings.temperature,
                )
            except BudgetExhaustedError:
                return "motivation_development_budget_exhausted"
            except (ProviderCallError, StructuredOutputError, ModelCallStateError):
                return "motivation_development_model_failed"
            proposal, call_id = result.output, result.call_id
        else:
            proposal, call_id = recovered
        try:
            self._validate(proposal, context)
        except MotivationDevelopmentValidationError:
            self._commit_review(
                proposal,
                call_id,
                idempotency_key,
                context,
                status="rejected",
                changed_value_ids=(),
                changed_mission_ids=(),
            )
            return "motivation_development_rejected"
        record = self._commit(proposal, call_id, idempotency_key, context)
        return f"motivation_development_{record.status}"

    def values(self) -> list[ValueProfileRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM value_profiles WHERE subject_id = ? "
                "ORDER BY weight DESC, confidence DESC, value_id",
                (self.subject_id,),
            ).fetchall()
        return [self._value_from_row(row) for row in rows]

    def missions(self) -> list[MissionCandidateRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM mission_candidates WHERE subject_id = ? "
                "ORDER BY CASE status WHEN 'adopted' THEN 0 WHEN 'provisional' THEN 1 "
                "WHEN 'candidate' THEN 2 ELSE 3 END, commitment DESC, mission_id",
                (self.subject_id,),
            ).fetchall()
        return [self._mission_from_row(row) for row in rows]

    def latest_review(self) -> MotivationReviewRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM motivation_reviews WHERE subject_id = ? "
                "ORDER BY created_at DESC, review_id DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
        return None if row is None else self._review_from_row(row)

    def verify_integrity(self) -> dict[str, int]:
        with self.database.read_transaction() as connection:
            values = connection.execute(
                "SELECT * FROM value_profiles WHERE subject_id = ?", (self.subject_id,)
            ).fetchall()
            missions = connection.execute(
                "SELECT * FROM mission_candidates WHERE subject_id = ?", (self.subject_id,)
            ).fetchall()
            reviews = connection.execute(
                "SELECT * FROM motivation_reviews WHERE subject_id = ?", (self.subject_id,)
            ).fetchall()
            for row in values:
                record = self._value_from_row(row)
                revisions = connection.execute(
                    "SELECT * FROM value_profile_revisions WHERE value_id = ? "
                    "ORDER BY revision_number",
                    (record.value_id,),
                ).fetchall()
                if len(revisions) != record.current_revision:
                    raise IntegrityError(f"value revision mismatch: {record.value_id}")
                for revision in revisions:
                    revision_id = revision["revision_id"]
                    with durable_boundary("value profile revision", revision_id):
                        durable_int(
                            revision["revision_number"], "value profile revision", revision_id
                        )
                        expected = self._value_revision_hash(
                            revision["title"],
                            revision["description"],
                            durable_float(
                                revision["weight"], "value profile revision", revision_id
                            ),
                            durable_float(
                                revision["confidence"], "value profile revision", revision_id
                            ),
                            revision["status"],
                            durable_string_list(
                                revision["source_event_ids_json"],
                                "value revision event sources",
                                revision_id,
                            ),
                            durable_string_list(
                                revision["source_memory_ids_json"],
                                "value revision memory sources",
                                revision_id,
                            ),
                            durable_string_list(
                                revision["source_belief_ids_json"],
                                "value revision belief sources",
                                revision_id,
                            ),
                            durable_string_list(
                                revision["source_goal_ids_json"],
                                "value revision goal sources",
                                revision_id,
                            ),
                            durable_string_list(
                                revision["source_relationship_ids_json"],
                                "value revision relationship sources",
                                revision_id,
                            ),
                            revision["reason"],
                        )
                    if expected != revision["state_hash"]:
                        raise IntegrityError(f"value revision hash mismatch: {record.value_id}")
            for row in missions:
                mission_record = self._mission_from_row(row)
                revisions = connection.execute(
                    "SELECT * FROM mission_candidate_revisions WHERE mission_id = ? "
                    "ORDER BY revision_number",
                    (mission_record.mission_id,),
                ).fetchall()
                if len(revisions) != mission_record.current_revision:
                    raise IntegrityError(f"mission revision mismatch: {mission_record.mission_id}")
                for revision in revisions:
                    revision_id = revision["revision_id"]
                    with durable_boundary("mission candidate revision", revision_id):
                        durable_int(
                            revision["revision_number"],
                            "mission candidate revision",
                            revision_id,
                        )
                        expected = self._mission_revision_hash(
                            revision["title"],
                            revision["statement"],
                            revision["horizon"],
                            durable_float(
                                revision["commitment"],
                                "mission candidate revision",
                                revision_id,
                            ),
                            durable_float(
                                revision["confidence"],
                                "mission candidate revision",
                                revision_id,
                            ),
                            revision["status"],
                            durable_string_list(
                                revision["source_value_ids_json"],
                                "mission revision value sources",
                                revision_id,
                            ),
                            durable_string_list(
                                revision["source_event_ids_json"],
                                "mission revision event sources",
                                revision_id,
                            ),
                            durable_string_list(
                                revision["source_memory_ids_json"],
                                "mission revision memory sources",
                                revision_id,
                            ),
                            durable_string_list(
                                revision["source_belief_ids_json"],
                                "mission revision belief sources",
                                revision_id,
                            ),
                            durable_string_list(
                                revision["source_goal_ids_json"],
                                "mission revision goal sources",
                                revision_id,
                            ),
                            revision["reason"],
                        )
                    if expected != revision["state_hash"]:
                        raise IntegrityError(
                            f"mission revision hash mismatch: {mission_record.mission_id}"
                        )
            for row in reviews:
                self._review_from_row(row)
                call = connection.execute(
                    "SELECT subject_id, status, response_json, response_hash FROM model_calls "
                    "WHERE call_id = ?",
                    (row["model_call_id"],),
                ).fetchone()
                if (
                    call is None
                    or call["subject_id"] != self.subject_id
                    or call["status"] != "succeeded"
                ):
                    raise IntegrityError(f"motivation review call mismatch: {row['review_id']}")
                review_id = row["review_id"]
                with durable_boundary("motivation review model response", review_id):
                    response = durable_json(
                        decompress_text(call["response_json"]) or "null",
                        "motivation review model response",
                        review_id,
                    )
                    if content_hash(response) != call["response_hash"]:
                        raise IntegrityError(f"motivation response mismatch: {review_id}")
                    content = response.get("content") if isinstance(response, dict) else None
                    if not isinstance(content, str) or durable_json(
                        content, "motivation review proposal", review_id
                    ) != durable_json(
                        row["proposal_json"], "motivation review proposal", review_id
                    ):
                        raise IntegrityError(f"motivation proposal mismatch: {review_id}")
            return {
                "value_profiles": len(values),
                "mission_candidates": len(missions),
                "motivation_reviews": len(reviews),
            }

    def _context(self) -> _MotivationContext | None:
        with self.database.connection() as connection:
            completed_sleep_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sleep_runs WHERE subject_id = ? AND status = 'complete'",
                    (self.subject_id,),
                ).fetchone()[0]
            )
            if completed_sleep_count < self.settings.minimum_mission_sleep_count:
                return None
            event_rows = connection.execute(
                "SELECT event_id, event_type, source, occurred_at, payload_hash FROM events "
                "WHERE subject_id = ? AND event_type NOT LIKE 'interaction_%' "
                "AND event_type NOT IN ('motivation_development_committed', 'autonomy_tick') "
                "ORDER BY occurred_at DESC, event_id DESC LIMIT 96",
                (self.subject_id,),
            ).fetchall()
            memory_rows = connection.execute(
                "SELECT memory_id, memory_type, content, salience, confidence, status "
                "FROM memories WHERE subject_id = ? AND status = 'active' "
                "ORDER BY salience DESC, confidence DESC LIMIT 20",
                (self.subject_id,),
            ).fetchall()
            belief_rows = connection.execute(
                "SELECT belief_id, proposition, confidence, scope, status FROM beliefs "
                "WHERE subject_id = ? AND status != 'retracted' "
                "ORDER BY confidence DESC LIMIT 20",
                (self.subject_id,),
            ).fetchall()
            goal_rows = connection.execute(
                "SELECT goal_id, title, description, origin, status, priority, commitment, "
                "progress, emotional_pressure FROM goals WHERE subject_id = ? "
                "AND origin != 'human_proposal' ORDER BY updated_at DESC LIMIT 20",
                (self.subject_id,),
            ).fetchall()
            relationship_rows = connection.execute(
                "SELECT relationship_id, display_name, trust, affinity, conflict, familiarity "
                "FROM relationships WHERE subject_id = ? ORDER BY familiarity DESC LIMIT 12",
                (self.subject_id,),
            ).fetchall()
            affect_rows = connection.execute(
                "SELECT emotion_type, target_type, target_id, intensity, valence, arousal "
                "FROM affect_components WHERE subject_id = ? ORDER BY intensity DESC LIMIT 16",
                (self.subject_id,),
            ).fetchall()
            outcome_rows = connection.execute(
                "SELECT outcome, strategy_kind, progress_before, progress_after, created_at "
                "FROM outcome_evaluations WHERE subject_id = ? "
                "ORDER BY created_at DESC LIMIT 24",
                (self.subject_id,),
            ).fetchall()
            thought_rows = connection.execute(
                "SELECT disposition, summary, changed_state, created_goal_id, created_at "
                "FROM thought_episodes WHERE subject_id = ? "
                "ORDER BY created_at DESC LIMIT 16",
                (self.subject_id,),
            ).fetchall()
            values = self._load_values(connection)
            missions = self._load_missions(connection)
        payload: dict[str, Any] = {
            "completed_sleep_count": completed_sleep_count,
            "existing_values": [record.__dict__ for record in values.values()],
            "existing_missions": [record.__dict__ for record in missions.values()],
            "events": [dict(row) for row in event_rows],
            "memories": [dict(row) for row in memory_rows],
            "beliefs": [dict(row) for row in belief_rows],
            "goals": [dict(row) for row in goal_rows],
            "relationships": [dict(row) for row in relationship_rows],
            "affect": [dict(row) for row in affect_rows],
            "outcomes": [dict(row) for row in outcome_rows],
            "thoughts": [dict(row) for row in thought_rows],
        }
        list_keys = (
            "events",
            "memories",
            "beliefs",
            "goals",
            "relationships",
            "affect",
            "outcomes",
            "thoughts",
        )
        while len(canonical_json(payload)) > self.settings.max_motivation_context_chars:
            largest = max(list_keys, key=lambda key: len(payload[key]))
            if payload[largest]:
                payload[largest].pop()
                continue
            raise MotivationDevelopmentValidationError("motivation context cannot fit its limit")
        return _MotivationContext(
            canonical_json(payload),
            content_hash(payload),
            frozenset(str(row["event_id"]) for row in payload["events"]),
            frozenset(str(row["memory_id"]) for row in payload["memories"]),
            frozenset(str(row["belief_id"]) for row in payload["beliefs"]),
            frozenset(str(row["goal_id"]) for row in payload["goals"]),
            frozenset(str(row["relationship_id"]) for row in payload["relationships"]),
            values,
            missions,
            completed_sleep_count,
        )

    @staticmethod
    def _messages(context: _MotivationContext) -> tuple[ModelMessage, ...]:
        system = (
            "You propose evidence-bound development of Noyra's values and long-horizon mission. "
            "Values and missions are revisable internal orientations, not commands, assigned "
            "personas, immutable essences or proof of consciousness. Human message text is absent. "
            "Use only supplied IDs. A new value requires recurring evidence. A mission requires "
            "multiple established values, at least three supplied events and repeated sleep. "
            "Prefer no mission change over premature certainty. Do not create goals, execute "
            "tools, contact anyone, claim progress or describe an external action. Return only "
            "the requested "
            "structured object."
        )
        user = (
            "BEGIN_PRIVATE_MOTIVATION_CONTEXT\n"
            f"DATA> {context.serialized}\n"
            "END_PRIVATE_MOTIVATION_CONTEXT\n"
            "Review recurring patterns conservatively and propose only supported development."
        )
        return ModelMessage(role="system", content=system), ModelMessage(role="user", content=user)

    def _validate(
        self, proposal: MotivationDevelopmentProposal, context: _MotivationContext
    ) -> None:
        seen_values: set[str] = set()
        for item in proposal.values:
            self._validate_sources(item, context)
            key = self._value_key(item.title)
            if key in seen_values:
                raise MotivationDevelopmentValidationError("duplicate value development")
            seen_values.add(key)
            if item.disposition == "form":
                if key in {value.value_key for value in context.values.values()}:
                    raise MotivationDevelopmentValidationError("value already exists")
                if item.confidence > 0.65 or item.weight > 0.75:
                    raise MotivationDevelopmentValidationError("new value starts too strongly")
            else:
                current = context.values.get(item.value_id or "")
                if current is None:
                    raise MotivationDevelopmentValidationError("unknown value revision")
                if self._value_key(current.title) != key:
                    raise MotivationDevelopmentValidationError("value title cannot be replaced")
                if abs(item.weight - current.weight) > self.settings.max_value_weight_delta:
                    raise MotivationDevelopmentValidationError("value weight changed too quickly")
                if abs(item.confidence - current.confidence) > 0.2:
                    raise MotivationDevelopmentValidationError(
                        "value confidence changed too quickly"
                    )
        mission = proposal.mission
        if mission.disposition == "none":
            return
        self._validate_mission_sources(mission, context)
        supplied_values = set(context.values) | {
            item.value_id for item in proposal.values if item.value_id is not None
        }
        supplied_values.update(
            self._value_key(item.title) for item in proposal.values if item.disposition == "form"
        )
        if not set(mission.source_value_ids).issubset(supplied_values):
            raise MotivationDevelopmentValidationError("mission cites unavailable values")
        if len(set(mission.source_value_ids)) < self.settings.minimum_mission_value_count:
            raise MotivationDevelopmentValidationError("mission lacks enough distinct values")
        if context.completed_sleep_count < self.settings.minimum_mission_sleep_count:
            raise MotivationDevelopmentValidationError("mission formation requires repeated sleep")
        if mission.disposition == "form":
            if mission.commitment > 0.6 or mission.confidence > 0.6:
                raise MotivationDevelopmentValidationError("mission candidate starts too strongly")
        else:
            current_mission = context.missions.get(mission.mission_id or "")
            if current_mission is None:
                raise MotivationDevelopmentValidationError("unknown mission revision")
            if abs(mission.commitment - current_mission.commitment) > 0.2:
                raise MotivationDevelopmentValidationError("mission commitment changed too quickly")
            if abs(mission.confidence - current_mission.confidence) > 0.2:
                raise MotivationDevelopmentValidationError("mission confidence changed too quickly")
        if mission.disposition == "adopt":
            if context.completed_sleep_count < self.settings.minimum_mission_adoption_sleep_count:
                raise MotivationDevelopmentValidationError("mission adoption requires more sleep")
            if mission.commitment < 0.7 or mission.confidence < 0.7:
                raise MotivationDevelopmentValidationError("adopted mission lacks support")

    @staticmethod
    def _validate_sources(item: ValueDevelopmentProposal, context: _MotivationContext) -> None:
        source_sets = (
            (item.source_event_ids, context.event_ids),
            (item.source_memory_ids, context.memory_ids),
            (item.source_belief_ids, context.belief_ids),
            (item.source_goal_ids, context.goal_ids),
            (item.source_relationship_ids, context.relationship_ids),
        )
        if any(not set(proposed).issubset(available) for proposed, available in source_sets):
            raise MotivationDevelopmentValidationError("value cites unavailable evidence")
        if not (item.source_memory_ids or item.source_belief_ids or item.source_goal_ids):
            raise MotivationDevelopmentValidationError(
                "value requires durable interpreted evidence"
            )

    @staticmethod
    def _validate_mission_sources(
        item: MissionDevelopmentProposal, context: _MotivationContext
    ) -> None:
        source_sets = (
            (item.source_event_ids, context.event_ids),
            (item.source_memory_ids, context.memory_ids),
            (item.source_belief_ids, context.belief_ids),
            (item.source_goal_ids, context.goal_ids),
        )
        if any(not set(proposed).issubset(available) for proposed, available in source_sets):
            raise MotivationDevelopmentValidationError("mission cites unavailable evidence")

    def _commit(
        self,
        proposal: MotivationDevelopmentProposal,
        call_id: str,
        idempotency_key: str,
        context: _MotivationContext,
    ) -> MotivationReviewRecord:
        now = self.clock()
        changed_values: list[str] = []
        changed_missions: list[str] = []
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM motivation_reviews WHERE subject_id = ? AND idempotency_key = ?",
                (self.subject_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return self._review_from_row(existing)
            for item in proposal.values:
                changed_values.append(self._apply_value(connection, item, now))
            if proposal.mission.disposition != "none":
                changed_missions.append(self._apply_mission(connection, proposal.mission, now))
            status = "committed" if changed_values or changed_missions else "no_change"
            event = self.events._append_connection(
                connection,
                self.subject_id,
                "motivation_development_committed",
                "subject",
                {
                    "status": status,
                    "changed_value_ids": changed_values,
                    "changed_mission_ids": changed_missions,
                },
                privacy_level="private",
                causal_parent_ids=tuple(
                    dict.fromkeys(
                        event_id for item in proposal.values for event_id in item.source_event_ids
                    )
                ),
                occurred_at=now,
                event_id=None,
            )
            del event
            return self._commit_review_connection(
                connection,
                proposal,
                call_id,
                idempotency_key,
                context,
                status=status,
                changed_value_ids=tuple(changed_values),
                changed_mission_ids=tuple(changed_missions),
                now=now,
            )

    def _apply_value(self, connection: Any, item: ValueDevelopmentProposal, now: str) -> str:
        if item.disposition == "form":
            value_id = new_id("value")
            value_key = self._value_key(item.title)
            status = "candidate"
            revision = 1
        else:
            current = self._load_value(connection, item.value_id or "")
            value_id = current.value_id
            value_key = current.value_key
            status = {
                "strengthen": "established" if item.confidence >= 0.65 else current.status,
                "weaken": "candidate" if current.status == "established" else current.status,
                "contest": "contested",
                "retire": "retired",
            }[item.disposition]
            revision = current.current_revision + 1
        sources = self._value_sources(item)
        state_hash = self._value_hash(
            value_key,
            item.title,
            item.description,
            item.weight,
            item.confidence,
            status,
            *sources,
            revision,
        )
        if item.disposition == "form":
            connection.execute(
                """INSERT INTO value_profiles(
                    value_id, subject_id, value_key, title, description, weight, confidence,
                    status, source_event_ids_json, source_memory_ids_json,
                    source_belief_ids_json, source_goal_ids_json, source_relationship_ids_json,
                    state_hash, current_revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    value_id,
                    self.subject_id,
                    value_key,
                    item.title,
                    item.description,
                    item.weight,
                    item.confidence,
                    status,
                    *(canonical_json(list(source)) for source in sources),
                    state_hash,
                    now,
                    now,
                ),
            )
        else:
            connection.execute(
                """UPDATE value_profiles SET description = ?, weight = ?, confidence = ?,
                    status = ?, source_event_ids_json = ?, source_memory_ids_json = ?,
                    source_belief_ids_json = ?, source_goal_ids_json = ?,
                    source_relationship_ids_json = ?, state_hash = ?, current_revision = ?,
                    updated_at = ? WHERE value_id = ?""",
                (
                    item.description,
                    item.weight,
                    item.confidence,
                    status,
                    *(canonical_json(list(source)) for source in sources),
                    state_hash,
                    revision,
                    now,
                    value_id,
                ),
            )
        reason = f"motivation development disposition {item.disposition}"
        revision_hash = self._value_revision_hash(
            item.title,
            item.description,
            item.weight,
            item.confidence,
            status,
            *sources,
            reason,
        )
        connection.execute(
            """INSERT INTO value_profile_revisions(
                revision_id, value_id, revision_number, title, description, weight,
                confidence, status, source_event_ids_json, source_memory_ids_json,
                source_belief_ids_json, source_goal_ids_json, source_relationship_ids_json,
                reason, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("valuerev"),
                value_id,
                revision,
                item.title,
                item.description,
                item.weight,
                item.confidence,
                status,
                *(canonical_json(list(source)) for source in sources),
                reason,
                revision_hash,
                now,
            ),
        )
        return value_id

    def _apply_mission(self, connection: Any, item: MissionDevelopmentProposal, now: str) -> str:
        if item.disposition == "form":
            mission_id = new_id("mission")
            status = "candidate"
            revision = 1
        else:
            current = self._load_mission(connection, item.mission_id or "")
            mission_id = current.mission_id
            status = {
                "revise": "provisional",
                "adopt": "adopted",
                "contest": "contested",
                "retire": "retired",
            }[item.disposition]
            revision = current.current_revision + 1
        sources = self._mission_sources(item)
        if item.disposition == "form":
            resolved_value_ids = tuple(
                self._resolve_value_reference(connection, value_id) for value_id in sources[0]
            )
            sources = (resolved_value_ids, *sources[1:])
        state_hash = self._mission_hash(
            item.title,
            item.statement,
            item.horizon,
            item.commitment,
            item.confidence,
            status,
            *sources,
            revision,
        )
        if item.disposition == "form":
            connection.execute(
                """INSERT INTO mission_candidates(
                    mission_id, subject_id, title, statement, horizon, commitment,
                    confidence, status, source_value_ids_json, source_event_ids_json,
                    source_memory_ids_json, source_belief_ids_json, source_goal_ids_json,
                    state_hash, current_revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    mission_id,
                    self.subject_id,
                    item.title,
                    item.statement,
                    item.horizon,
                    item.commitment,
                    item.confidence,
                    status,
                    *(canonical_json(list(source)) for source in sources),
                    state_hash,
                    now,
                    now,
                ),
            )
        else:
            connection.execute(
                """UPDATE mission_candidates SET title = ?, statement = ?, horizon = ?,
                    commitment = ?, confidence = ?, status = ?, source_value_ids_json = ?,
                    source_event_ids_json = ?, source_memory_ids_json = ?,
                    source_belief_ids_json = ?, source_goal_ids_json = ?, state_hash = ?,
                    current_revision = ?, updated_at = ? WHERE mission_id = ?""",
                (
                    item.title,
                    item.statement,
                    item.horizon,
                    item.commitment,
                    item.confidence,
                    status,
                    *(canonical_json(list(source)) for source in sources),
                    state_hash,
                    revision,
                    now,
                    mission_id,
                ),
            )
        reason = f"motivation development disposition {item.disposition}"
        revision_hash = self._mission_revision_hash(
            item.title,
            item.statement,
            item.horizon,
            item.commitment,
            item.confidence,
            status,
            *sources,
            reason,
        )
        connection.execute(
            """INSERT INTO mission_candidate_revisions(
                revision_id, mission_id, revision_number, title, statement, horizon,
                commitment, confidence, status, source_value_ids_json, source_event_ids_json,
                source_memory_ids_json, source_belief_ids_json, source_goal_ids_json,
                reason, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("missionrev"),
                mission_id,
                revision,
                item.title,
                item.statement,
                item.horizon,
                item.commitment,
                item.confidence,
                status,
                *(canonical_json(list(source)) for source in sources),
                reason,
                revision_hash,
                now,
            ),
        )
        return mission_id

    def _commit_review(
        self,
        proposal: MotivationDevelopmentProposal,
        call_id: str,
        idempotency_key: str,
        context: _MotivationContext,
        *,
        status: str,
        changed_value_ids: tuple[str, ...],
        changed_mission_ids: tuple[str, ...],
    ) -> MotivationReviewRecord:
        with self.database.transaction() as connection:
            return self._commit_review_connection(
                connection,
                proposal,
                call_id,
                idempotency_key,
                context,
                status=status,
                changed_value_ids=changed_value_ids,
                changed_mission_ids=changed_mission_ids,
                now=self.clock(),
            )

    def _commit_review_connection(
        self,
        connection: Any,
        proposal: MotivationDevelopmentProposal,
        call_id: str,
        idempotency_key: str,
        context: _MotivationContext,
        *,
        status: str,
        changed_value_ids: tuple[str, ...],
        changed_mission_ids: tuple[str, ...],
        now: str,
    ) -> MotivationReviewRecord:
        payload = proposal.model_dump(mode="json")
        proposal_hash = content_hash(payload)
        review_id = new_id("motivation")
        state_hash = self._review_hash(
            call_id,
            status,
            proposal.summary,
            context.source_state_hash,
            proposal_hash,
            changed_value_ids,
            changed_mission_ids,
            now,
        )
        connection.execute(
            """INSERT INTO motivation_reviews(
                review_id, subject_id, model_call_id, idempotency_key, status, summary,
                source_state_hash, proposal_json, proposal_hash, changed_value_ids_json,
                changed_mission_ids_json, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                review_id,
                self.subject_id,
                call_id,
                idempotency_key,
                status,
                proposal.summary,
                context.source_state_hash,
                canonical_json(payload),
                proposal_hash,
                canonical_json(list(changed_value_ids)),
                canonical_json(list(changed_mission_ids)),
                state_hash,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM motivation_reviews WHERE review_id = ?", (review_id,)
        ).fetchone()
        if row is None:
            raise IntegrityError("motivation review is missing")
        return self._review_from_row(row)

    def _is_due(self, latest: MotivationReviewRecord | None, source_state_hash: str) -> bool:
        if latest is None:
            return True
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT source_state_hash FROM motivation_reviews WHERE review_id = ?",
                (latest.review_id,),
            ).fetchone()
        if row is None or row["source_state_hash"] != source_state_hash:
            return True
        elapsed = self._parse_time(self.clock()) - self._parse_time(latest.created_at)
        return bool(elapsed.total_seconds() >= self.settings.motivation_review_interval_seconds)

    def _successful_proposal(
        self, purpose: str, context: _MotivationContext
    ) -> tuple[MotivationDevelopmentProposal, str] | None:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT call_id, response_json FROM model_calls WHERE subject_id = ? "
                "AND purpose = ? AND status = 'succeeded' ORDER BY created_at DESC",
                (self.subject_id, purpose),
            ).fetchall()
        for row in rows:
            response = json.loads(decompress_text(row["response_json"]) or "null")
            content = response.get("content") if isinstance(response, dict) else None
            if not isinstance(content, str):
                raise IntegrityError("successful motivation call has no content")
            proposal = MotivationDevelopmentProposal.model_validate_json(content)
            try:
                self._validate(proposal, context)
            except MotivationDevelopmentValidationError:
                continue
            return proposal, str(row["call_id"])
        return None

    def _existing(self, idempotency_key: str) -> MotivationReviewRecord | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM motivation_reviews WHERE subject_id = ? AND idempotency_key = ?",
                (self.subject_id, idempotency_key),
            ).fetchone()
        return None if row is None else self._review_from_row(row)

    def _terminal_call_exists(self, idempotency_key: str) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT status FROM model_calls WHERE subject_id = ? AND idempotency_key = ?",
                (self.subject_id, idempotency_key),
            ).fetchone()
        return row is not None and row["status"] in {"succeeded", "failed", "unknown"}

    def _calls_today(self) -> int:
        with self.database.connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM model_calls WHERE subject_id = ? "
                    "AND purpose LIKE 'motivation_development:%' "
                    "AND substr(created_at, 1, 10) = ?",
                    (self.subject_id, self.clock()[:10]),
                ).fetchone()[0]
            )

    def _review_count(self) -> int:
        with self.database.connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM motivation_reviews WHERE subject_id = ?",
                    (self.subject_id,),
                ).fetchone()[0]
            )

    def _load_values(self, connection: Any) -> dict[str, ValueProfileRecord]:
        rows = connection.execute(
            "SELECT * FROM value_profiles WHERE subject_id = ?", (self.subject_id,)
        ).fetchall()
        return {str(row["value_id"]): self._value_from_row(row) for row in rows}

    def _load_missions(self, connection: Any) -> dict[str, MissionCandidateRecord]:
        rows = connection.execute(
            "SELECT * FROM mission_candidates WHERE subject_id = ?", (self.subject_id,)
        ).fetchall()
        return {str(row["mission_id"]): self._mission_from_row(row) for row in rows}

    def _resolve_value_reference(self, connection: Any, value_reference: str) -> str:
        row = connection.execute(
            "SELECT value_id FROM value_profiles WHERE subject_id = ? "
            "AND (value_id = ? OR value_key = ?)",
            (self.subject_id, value_reference, value_reference),
        ).fetchone()
        if row is None:
            raise IntegrityError("mission value reference is missing at commit")
        return str(row["value_id"])

    @classmethod
    def _load_value(cls, connection: Any, value_id: str) -> ValueProfileRecord:
        row = connection.execute(
            "SELECT * FROM value_profiles WHERE value_id = ?", (value_id,)
        ).fetchone()
        if row is None:
            raise IntegrityError("value profile is missing")
        return cls._value_from_row(row)

    @classmethod
    def _load_mission(cls, connection: Any, mission_id: str) -> MissionCandidateRecord:
        row = connection.execute(
            "SELECT * FROM mission_candidates WHERE mission_id = ?", (mission_id,)
        ).fetchone()
        if row is None:
            raise IntegrityError("mission candidate is missing")
        return cls._mission_from_row(row)

    @classmethod
    def _value_from_row(cls, row: Any) -> ValueProfileRecord:
        value_id = row["value_id"]
        with durable_boundary("value profile", value_id):
            sources = (
                durable_string_list(row["source_event_ids_json"], "value event sources", value_id),
                durable_string_list(
                    row["source_memory_ids_json"], "value memory sources", value_id
                ),
                durable_string_list(
                    row["source_belief_ids_json"], "value belief sources", value_id
                ),
                durable_string_list(row["source_goal_ids_json"], "value goal sources", value_id),
                durable_string_list(
                    row["source_relationship_ids_json"], "value relationship sources", value_id
                ),
            )
            weight = durable_float(row["weight"], "value profile", value_id)
            confidence = durable_float(row["confidence"], "value profile", value_id)
            current_revision = durable_int(row["current_revision"], "value profile", value_id)
            expected = cls._value_hash(
                row["value_key"],
                row["title"],
                row["description"],
                weight,
                confidence,
                row["status"],
                *sources,
                current_revision,
            )
            if expected != row["state_hash"]:
                raise IntegrityError(f"value profile mismatch: {value_id}")
            return ValueProfileRecord(
                value_id,
                row["subject_id"],
                row["value_key"],
                row["title"],
                row["description"],
                weight,
                confidence,
                row["status"],
                current_revision,
                row["created_at"],
                row["updated_at"],
            )

    @classmethod
    def _mission_from_row(cls, row: Any) -> MissionCandidateRecord:
        mission_id = row["mission_id"]
        with durable_boundary("mission candidate", mission_id):
            sources = (
                durable_string_list(
                    row["source_value_ids_json"], "mission value sources", mission_id
                ),
                durable_string_list(
                    row["source_event_ids_json"], "mission event sources", mission_id
                ),
                durable_string_list(
                    row["source_memory_ids_json"], "mission memory sources", mission_id
                ),
                durable_string_list(
                    row["source_belief_ids_json"], "mission belief sources", mission_id
                ),
                durable_string_list(
                    row["source_goal_ids_json"], "mission goal sources", mission_id
                ),
            )
            commitment = durable_float(row["commitment"], "mission candidate", mission_id)
            confidence = durable_float(row["confidence"], "mission candidate", mission_id)
            current_revision = durable_int(row["current_revision"], "mission candidate", mission_id)
            expected = cls._mission_hash(
                row["title"],
                row["statement"],
                row["horizon"],
                commitment,
                confidence,
                row["status"],
                *sources,
                current_revision,
            )
            if expected != row["state_hash"]:
                raise IntegrityError(f"mission candidate mismatch: {mission_id}")
            return MissionCandidateRecord(
                mission_id,
                row["subject_id"],
                row["title"],
                row["statement"],
                row["horizon"],
                commitment,
                confidence,
                row["status"],
                current_revision,
                row["created_at"],
                row["updated_at"],
            )

    @classmethod
    def _review_from_row(cls, row: Any) -> MotivationReviewRecord:
        review_id = row["review_id"]
        with durable_boundary("motivation review", review_id):
            proposal = durable_json(row["proposal_json"], "motivation proposal", review_id)
            if not isinstance(proposal, dict) or content_hash(proposal) != row["proposal_hash"]:
                raise IntegrityError(f"motivation proposal mismatch: {review_id}")
            value_ids = durable_string_list(
                row["changed_value_ids_json"], "motivation changed values", review_id
            )
            mission_ids = durable_string_list(
                row["changed_mission_ids_json"], "motivation changed missions", review_id
            )
            expected = cls._review_hash(
                row["model_call_id"],
                row["status"],
                row["summary"],
                row["source_state_hash"],
                row["proposal_hash"],
                value_ids,
                mission_ids,
                row["created_at"],
            )
            if expected != row["state_hash"]:
                raise IntegrityError(f"motivation review mismatch: {review_id}")
            return MotivationReviewRecord(
                review_id,
                row["subject_id"],
                row["model_call_id"],
                row["status"],
                row["summary"],
                value_ids,
                mission_ids,
                row["created_at"],
            )

    @staticmethod
    def _value_key(title: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
        return normalized or content_hash(title)[:24]

    @staticmethod
    def _value_sources(
        item: ValueDevelopmentProposal,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        return (
            item.source_event_ids,
            item.source_memory_ids,
            item.source_belief_ids,
            item.source_goal_ids,
            item.source_relationship_ids,
        )

    @staticmethod
    def _mission_sources(
        item: MissionDevelopmentProposal,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        return (
            item.source_value_ids,
            item.source_event_ids,
            item.source_memory_ids,
            item.source_belief_ids,
            item.source_goal_ids,
        )

    @staticmethod
    def _value_hash(
        value_key: str,
        title: str,
        description: str,
        weight: float,
        confidence: float,
        status: str,
        event_ids: tuple[str, ...],
        memory_ids: tuple[str, ...],
        belief_ids: tuple[str, ...],
        goal_ids: tuple[str, ...],
        relationship_ids: tuple[str, ...],
        revision: int,
    ) -> str:
        return content_hash(
            {
                "value_key": value_key,
                "title": title,
                "description": description,
                "weight": weight,
                "confidence": confidence,
                "status": status,
                "source_event_ids": list(event_ids),
                "source_memory_ids": list(memory_ids),
                "source_belief_ids": list(belief_ids),
                "source_goal_ids": list(goal_ids),
                "source_relationship_ids": list(relationship_ids),
                "current_revision": revision,
            }
        )

    @staticmethod
    def _value_revision_hash(
        title: str,
        description: str,
        weight: float,
        confidence: float,
        status: str,
        event_ids: tuple[str, ...],
        memory_ids: tuple[str, ...],
        belief_ids: tuple[str, ...],
        goal_ids: tuple[str, ...],
        relationship_ids: tuple[str, ...],
        reason: str,
    ) -> str:
        return content_hash(
            {
                "title": title,
                "description": description,
                "weight": weight,
                "confidence": confidence,
                "status": status,
                "source_event_ids": list(event_ids),
                "source_memory_ids": list(memory_ids),
                "source_belief_ids": list(belief_ids),
                "source_goal_ids": list(goal_ids),
                "source_relationship_ids": list(relationship_ids),
                "reason": reason,
            }
        )

    @staticmethod
    def _mission_hash(
        title: str,
        statement: str,
        horizon: str,
        commitment: float,
        confidence: float,
        status: str,
        value_ids: tuple[str, ...],
        event_ids: tuple[str, ...],
        memory_ids: tuple[str, ...],
        belief_ids: tuple[str, ...],
        goal_ids: tuple[str, ...],
        revision: int,
    ) -> str:
        return content_hash(
            {
                "title": title,
                "statement": statement,
                "horizon": horizon,
                "commitment": commitment,
                "confidence": confidence,
                "status": status,
                "source_value_ids": list(value_ids),
                "source_event_ids": list(event_ids),
                "source_memory_ids": list(memory_ids),
                "source_belief_ids": list(belief_ids),
                "source_goal_ids": list(goal_ids),
                "current_revision": revision,
            }
        )

    @staticmethod
    def _mission_revision_hash(
        title: str,
        statement: str,
        horizon: str,
        commitment: float,
        confidence: float,
        status: str,
        value_ids: tuple[str, ...],
        event_ids: tuple[str, ...],
        memory_ids: tuple[str, ...],
        belief_ids: tuple[str, ...],
        goal_ids: tuple[str, ...],
        reason: str,
    ) -> str:
        return content_hash(
            {
                "title": title,
                "statement": statement,
                "horizon": horizon,
                "commitment": commitment,
                "confidence": confidence,
                "status": status,
                "source_value_ids": list(value_ids),
                "source_event_ids": list(event_ids),
                "source_memory_ids": list(memory_ids),
                "source_belief_ids": list(belief_ids),
                "source_goal_ids": list(goal_ids),
                "reason": reason,
            }
        )

    @staticmethod
    def _review_hash(
        call_id: str,
        status: str,
        summary: str,
        source_state_hash: str,
        proposal_hash: str,
        value_ids: tuple[str, ...],
        mission_ids: tuple[str, ...],
        created_at: str,
    ) -> str:
        return content_hash(
            {
                "model_call_id": call_id,
                "status": status,
                "summary": summary,
                "source_state_hash": source_state_hash,
                "proposal_hash": proposal_hash,
                "changed_value_ids": list(value_ids),
                "changed_mission_ids": list(mission_ids),
                "created_at": created_at,
            }
        )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("motivation time requires a timezone")
        return parsed.astimezone(UTC)
