"""Bounded candidate reading, separate from search snippets and source polling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from noyra.capability import CapabilityStore, ToolRunner
from noyra.capability.errors import CapabilityDeniedError
from noyra.core.actions import ActionLedger
from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import content_hash
from noyra.world import ObservationStore, SafeWebReader, SourceRecord, SourceRegistry
from noyra.world.errors import UnsafeSourceError, WorldStateConflictError
from noyra.world.poll import SourcePollStore

READING_FORMAT = "noyra-research-reading/v1"


@dataclass(frozen=True)
class Reading:
    source: SourceRecord
    receipt: dict[str, Any]
    excerpt: str
    links: tuple[str, ...]
    signals: tuple[str, ...]


@dataclass(frozen=True)
class ReadOutcome:
    reading: Reading | None
    status: str
    action_id: str | None = None


class CandidateExplorer:
    def __init__(self, database: Database, subject_id: str, reader: SafeWebReader):
        self.database = database
        self.subject_id = subject_id
        self.reader = reader
        self.sources = SourceRegistry(database)
        self.tools = ToolRunner(database)
        self.capabilities = CapabilityStore(database)
        self.observations = ObservationStore(database)
        self.polls = SourcePollStore(database)

    def candidate(self, url: str, title: str, method: str) -> SourceRecord | None:
        # Reuse the registered identity; search titles must not rename it or
        # overwrite an operator block/revision.
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT source_id FROM world_sources WHERE subject_id=? AND url=?",
                (self.subject_id, url),
            ).fetchone()
        if row is not None:
            record = self.sources.get(str(row[0]), subject_id=self.subject_id)
            return None if record.status == "blocked" else record
        try:
            return self.sources.register(
                self.subject_id,
                title.strip()[:256] or url[:256],
                url,
                "web",
                trust_score=0.35,
                status="candidate",
                reason=f"autonomous {method} discovery; not yet read",
            )
        except (UnsafeSourceError, WorldStateConflictError):
            return None

    def attention_budget_available(self, action_id: str) -> bool:
        """New assessment calls also respect the linked project model envelope."""
        with self.database.connection() as connection:
            project = connection.execute(
                "SELECT p.* FROM autonomous_projects p JOIN actions a "
                "ON a.project_id=p.project_id WHERE p.subject_id=? AND a.action_id=?",
                (self.subject_id, action_id),
            ).fetchone()
            if project is None:
                return True
            project_id = str(project["project_id"])
            calls = connection.execute(
                "SELECT COUNT(*) FROM model_calls c WHERE c.subject_id=? AND ("
                "c.call_id=? OR c.call_id IN (SELECT u.source_id FROM "
                "autonomous_project_resource_uses u WHERE u.project_id=? "
                "AND u.source_type='model_call') OR c.purpose LIKE ? OR c.purpose LIKE ? "
                "OR c.purpose LIKE ? OR EXISTS(SELECT 1 FROM model_calls planner "
                "WHERE planner.subject_id=c.subject_id AND planner.purpose LIKE ? "
                "AND c.purpose IN ('research_reading:' || planner.call_id || ':1', "
                "'research_reading:' || planner.call_id || ':2')))",
                (
                    self.subject_id,
                    project["formation_call_id"],
                    project_id,
                    f"autonomous_project_review:{project_id}:%",
                    f"autonomous_project_artifact:{project_id}:%",
                    f"research_plan:%:{project_id}:%",
                    f"research_plan:%:{project_id}:%",
                ),
            ).fetchone()[0]
        return bool(calls < project["max_model_calls"])

    async def read(
        self,
        source: SourceRecord,
        *,
        planner_call_id: str,
        goal_id: str,
        project_id: str | None,
        phase_id: str | None,
        now: str,
        cooldown_seconds: float,
        max_chars: int,
    ) -> ReadOutcome:
        key = f"research-trial:{planner_call_id}:{content_hash(source.url)}"
        with self.database.connection() as connection:
            previous = connection.execute(
                "SELECT action_id FROM actions WHERE subject_id=? AND idempotency_key=?",
                (self.subject_id, key),
            ).fetchone()
        current = self.sources.get(source.source_id, subject_id=self.subject_id)
        if current.status == "blocked" or current.url != source.url:
            return ReadOutcome(None, "source_unavailable")
        if previous is None and project_id is not None:
            with self.database.connection() as connection:
                project = connection.execute(
                    "SELECT p.max_external_actions, p.status, ph.status AS phase_status "
                    "FROM autonomous_projects p JOIN autonomous_project_phases ph "
                    "ON ph.project_id=p.project_id WHERE p.subject_id=? AND p.project_id=? "
                    "AND ph.phase_id=?",
                    (self.subject_id, project_id, phase_id),
                ).fetchone()
                uses = connection.execute(
                    "SELECT COUNT(*) FROM actions WHERE subject_id=? AND project_id=?",
                    (self.subject_id, project_id),
                ).fetchone()[0]
            if (
                project is None
                or project["status"] not in {"active", "planned"}
                or project["phase_status"] != "active"
            ):
                return ReadOutcome(None, "project_unavailable")
            if uses >= project["max_external_actions"]:
                return ReadOutcome(None, "project_action_budget")
        if previous is None:
            checked = self.polls.last_checked_at(self.subject_id, source.source_id)
            # Future timestamps hold the cooldown rather than granting a retry.
            if (
                checked is not None
                and (datetime.fromisoformat(now) - datetime.fromisoformat(checked)).total_seconds()
                < cooldown_seconds
            ):
                return ReadOutcome(None, "cooldown")
        try:
            result = await self.tools.fetch_document(
                self.subject_id,
                source,
                self.reader,
                candidate_trial=True,
                goal_id=goal_id,
                project_id=project_id,
                phase_id=phase_id,
                idempotency_key=key,
                public_goal_reference=goal_id,
                max_observation_chars=max_chars,
            )
        except (PermissionError, CapabilityDeniedError):
            return ReadOutcome(None, "capability_unavailable")
        with self.database.connection() as connection:
            action = ActionLedger._load_connection(connection, result.action_id)
        payload = action.result or {}
        observation_id = payload.get("observation_id")
        if action.status != "succeeded" or not isinstance(observation_id, str):
            return ReadOutcome(
                None,
                action.status if action.status != "succeeded" else "no_observation",
                action.action_id,
            )
        if payload.get("observation_created") is False:
            return ReadOutcome(None, "unchanged", action.action_id)
        observation = self.observations.get(observation_id, subject_id=self.subject_id)
        if observation.source_id != source.source_id or observation.content_hash != payload.get(
            "observation_hash"
        ):
            raise IntegrityError("candidate reading evidence mismatch")
        raw_links = payload.get("links", [])
        if not isinstance(raw_links, list) or any(not isinstance(link, str) for link in raw_links):
            raise IntegrityError("candidate reading links are invalid")
        receipt = {
            "source_id": source.source_id,
            "action_id": action.action_id,
            "observation_id": observation_id,
            "content_hash": observation.content_hash,
        }
        reading = Reading(
            source,
            receipt,
            observation.content[:6_000],
            tuple(raw_links[:16]),
            observation.injection_signals,
        )

        return ReadOutcome(reading, "read", action.action_id)


def verify_reading_evidence(
    connection: Any,
    subject_id: str,
    rounds: list[dict[str, Any]],
    accepted: list[str],
    *,
    goal_id: str | None,
    project_id: str | None,
    phase_id: str | None,
) -> bool:
    """New runs require actual read receipts; historical discovery runs keep their contract."""
    marked = [item for item in rounds if item.get("reading_format") == READING_FORMAT]
    if any(
        "reading_format" in item and item["reading_format"] != READING_FORMAT for item in rounds
    ):
        raise IntegrityError("unsupported research reading format")
    if not marked:
        return False
    if len(marked) != 1 or marked[0] is not rounds[-1]:
        raise IntegrityError("research reading marker is out of order")
    receipts = marked[-1].get("readings")
    if not isinstance(receipts, list) or len(receipts) > 12:
        raise IntegrityError("research reading receipts are invalid")
    verified: set[str] = set()
    seen_actions: set[str] = set()
    seen_observations: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise IntegrityError("research reading receipt is invalid")
        if set(receipt) != {"source_id", "action_id", "observation_id", "content_hash"} or any(
            not isinstance(value, str) or not value for value in receipt.values()
        ):
            raise IntegrityError("research reading receipt fields are invalid")
        action_id = str(receipt["action_id"])
        observation_id = str(receipt["observation_id"])
        if action_id in seen_actions or observation_id in seen_observations:
            raise IntegrityError("research reading receipts contain duplicate evidence")
        seen_actions.add(action_id)
        seen_observations.add(observation_id)
        observation = connection.execute(
            "SELECT subject_id, source_id, canonical_url, content_hash, injection_signals_json "
            "FROM observations "
            "WHERE observation_id=?",
            (receipt.get("observation_id"),),
        ).fetchone()
        try:
            action = ActionLedger._load_connection(connection, action_id)
        except NotFoundError as error:
            raise IntegrityError("research reading action is missing") from error
        payload = action.result or {}
        authorized = connection.execute(
            "SELECT 1 FROM capability_uses u JOIN capability_grants g ON g.grant_id=u.grant_id "
            "WHERE u.subject_id=? AND u.action_id=? AND u.resource=? "
            "AND g.subject_id=u.subject_id AND g.capability_type='web_read' LIMIT 1",
            (subject_id, action.action_id, action.target),
        ).fetchone()
        if (
            observation is None
            or authorized is None
            or observation["subject_id"] != subject_id
            or observation["source_id"] != receipt.get("source_id")
            or observation["content_hash"] != receipt.get("content_hash")
            or action.subject_id != subject_id
            or action.status != "succeeded"
            or action.target != observation["canonical_url"]
            or action.action_type != "web_read"
            or action.tool != "safe_web_reader"
            or action.goal_id != goal_id
            or action.project_id != project_id
            or action.phase_id != phase_id
            or payload.get("observation_id") != receipt.get("observation_id")
            or payload.get("observation_hash") != receipt.get("content_hash")
            or payload.get("observation_created") is not True
        ):
            raise IntegrityError("research reading is not backed by an authorized observation")
        # Signals prevent promotion/acceptance, not the recording of what was read.
        if observation["injection_signals_json"] == "[]":
            verified.add(str(receipt["source_id"]))
    if not set(accepted).issubset(verified):
        raise IntegrityError("research accepted a source without clean reading evidence")
    return True
