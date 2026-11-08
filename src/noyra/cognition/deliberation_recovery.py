"""Original admission evidence for recovering paid action deliberation proposals."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any, Literal

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.events import EventStore
from noyra.core.private_sources import assert_event_sources_available
from noyra.core.types import content_hash, strict_json_loads
from noyra.mind import GoalStore
from noyra.model import ModelLedger
from noyra.model.types import CompletionRequest
from noyra.world import SourceRegistry

from .types import ActionDeliberationProposal

FORMAT = "action-deliberation-context/v1"


class DeliberationRecovery:
    def __init__(self, database: Database, subject_id: str):
        self.database, self.subject_id = database, subject_id
        self.events = EventStore(database)

    @staticmethod
    def event_id(call_id: str) -> str:
        return "evt_action_deliberation_context_" + call_id

    def bind(
        self,
        connection: Any,
        call: Any,
        context: Any,
        *,
        max_output_tokens: int,
        temperature: float,
    ) -> None:
        from .deliberation import ActionDeliberation

        if call.subject_id != self.subject_id or not call.purpose.startswith(
            "action_deliberation:"
        ):
            raise IntegrityError("action deliberation context crosses model ownership")
        messages = ActionDeliberation._messages(context)
        efforts: tuple[Literal["low", "medium", "high", "xhigh"] | None, ...] = (
            None,
            "low",
            "medium",
            "high",
            "xhigh",
        )
        hashes = {
            content_hash(
                CompletionRequest(
                    model=call.model,
                    messages=messages,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                    schema_name=ActionDeliberationProposal.__name__,
                    output_schema=ActionDeliberationProposal.model_json_schema(),
                    reasoning_effort=effort,
                ).model_dump(mode="json")
            )
            for effort in efforts
        }
        if call.request_hash not in hashes:
            raise IntegrityError("action deliberation model request differs from bound context")
        serialized = strict_json_loads(context.serialized)
        events = serialized["recent_events"]
        if {e["event_id"] for e in events} != set(context.event_ids):
            raise IntegrityError("action deliberation supplied event identities differ")
        payload = {
            "format": FORMAT,
            "subject_id": self.subject_id,
            "call_id": call.call_id,
            "purpose": call.purpose,
            "request_hash": call.request_hash,
            "context_hash": content_hash(serialized),
            "goals": {key: content_hash(asdict(goal)) for key, goal in context.goals.items()},
            "sources": {
                key: content_hash(asdict(source)) for key, source in context.sources.items()
            },
            "events": events,
        }
        self.events._append_connection(
            connection,
            self.subject_id,
            "action_deliberation_context_bound",
            "action_supervisor",
            payload,
            privacy_level="private",
            causal_parent_ids=(),
            occurred_at=None,
            event_id=self.event_id(call.call_id),
        )

    def read(self, connection: Any, call: Any) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM events WHERE event_id=?", (self.event_id(call.call_id),)
        ).fetchone()
        if row is None:
            return None
        value = self.events.payload_from_row(row, connection=connection)
        if (
            row["subject_id"] != self.subject_id
            or row["event_type"] != "action_deliberation_context_bound"
            or row["source"] != "action_supervisor"
            or row["privacy_level"] != "private"
            or content_hash(value) != row["payload_hash"]
            or set(value)
            != {
                "format",
                "subject_id",
                "call_id",
                "purpose",
                "request_hash",
                "context_hash",
                "goals",
                "sources",
                "events",
            }
            or value["format"] != FORMAT
            or value["subject_id"] != self.subject_id
            or call.subject_id != self.subject_id
            or value["call_id"] != call.call_id
            or value["purpose"] != call.purpose
            or value["request_hash"] != call.request_hash
            or not self.digest(value["context_hash"])
            or any(
                not isinstance(value[key], dict) or len(value[key]) > 16
                for key in ("goals", "sources")
            )
            or any(
                not isinstance(key, str) or not key or not self.digest(digest)
                for key in ("goals", "sources")
                for key, digest in value[key].items()
            )
            or not isinstance(value["events"], list)
            or len(value["events"]) > 48
        ):
            raise IntegrityError("action deliberation original context evidence is invalid")
        for event in value["events"]:
            if (
                not isinstance(event, dict)
                or set(event) != {"event_id", "event_type", "source", "occurred_at", "payload_hash"}
                or any(not isinstance(v, str) or not v for v in event.values())
                or not self.digest(event["payload_hash"])
            ):
                raise IntegrityError("action deliberation original event metadata is invalid")
        if len({e["event_id"] for e in value["events"]}) != len(value["events"]):
            raise IntegrityError("action deliberation original evidence is duplicated")
        return value

    @staticmethod
    def digest(value: Any) -> bool:
        return (
            isinstance(value, str) and len(value) == 64 and not set(value) - set("0123456789abcdef")
        )

    def context(self, call: Any, proposal: ActionDeliberationProposal, current: Any) -> Any:
        from .deliberation import ActionDeliberationValidationError

        with self.database.read_transaction() as connection:
            bound = self.read(connection, call)
            if bound is None:
                # Pre-upgrade calls have no trustworthy frozen context. Preserve
                # their prior current-evidence validation rather than inventing it.
                return current
            goals = {}
            sources = {}
            try:
                if proposal.goal_id is not None and proposal.goal_id in bound["goals"]:
                    goal = GoalStore(self.database).get(proposal.goal_id)
                    if (
                        goal.subject_id != self.subject_id
                        or content_hash(asdict(goal)) != bound["goals"][proposal.goal_id]
                    ):
                        raise ActionDeliberationValidationError("bound action goal changed")
                    goals[goal.goal_id] = goal
                if proposal.source_id is not None and proposal.source_id in bound["sources"]:
                    source = SourceRegistry(self.database).get(
                        proposal.source_id, subject_id=self.subject_id
                    )
                    if content_hash(asdict(source)) != bound["sources"][proposal.source_id]:
                        raise ActionDeliberationValidationError("bound action source changed")
                    sources[source.source_id] = source
            except NotFoundError as error:
                raise ActionDeliberationValidationError(
                    "bound action input is unavailable"
                ) from error
            for evidence in bound["events"]:
                event = connection.execute(
                    "SELECT event_id,event_type,source,occurred_at,payload_hash "
                    "FROM events WHERE subject_id=? AND event_id=?",
                    (self.subject_id, evidence["event_id"]),
                ).fetchone()
                if event is None or dict(event) != evidence:
                    raise ActionDeliberationValidationError("bound action evidence changed")
                try:
                    assert_event_sources_available(
                        connection, self.subject_id, evidence["event_id"]
                    )
                except PermissionError as error:
                    raise ActionDeliberationValidationError(
                        "bound action evidence was withdrawn"
                    ) from error
        return replace(
            current,
            goals=goals,
            sources=sources,
            event_ids=frozenset(e["event_id"] for e in bound["events"]),
        )

    def verify_connection(self, connection: Any) -> int:
        count = 0
        prefix = "action_deliberation:"
        for row in connection.execute(
            "SELECT c.* FROM model_calls c JOIN events e ON e.event_id=?||c.call_id "
            "WHERE c.subject_id=? AND substr(c.purpose,1,?)=?",
            ("evt_action_deliberation_context_", self.subject_id, len(prefix), prefix),
        ):
            self.read(connection, ModelLedger._call_from_row(row))
            count += 1
        return count
