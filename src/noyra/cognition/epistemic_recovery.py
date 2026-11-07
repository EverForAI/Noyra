"""Original evidence for local recovery of paid epistemic proposals."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.events import EventStore
from noyra.core.private_sources import assert_event_sources_available
from noyra.core.types import content_hash, strict_json_loads
from noyra.mind import BeliefStore
from noyra.model import ModelLedger
from noyra.model.request_identity import UNRUNNABLE_BUSINESS_CALL_SQL
from noyra.model.types import CompletionRequest
from noyra.world import ObservationStore, PredictionStore

FORMAT = "epistemic-context/v1"
BELIEF_FIELDS = "belief_id,proposition,confidence,scope,status,current_revision"
PREDICTION_FIELDS = "prediction_id,statement,probability,target_at,resolution_criteria,status"


class EpistemicContextUnavailable(ValueError):
    """Paid evidence cannot safely be adopted; never permission to repay."""


class EpistemicRecovery:
    def __init__(self, database: Database, subject_id: str):
        self.database, self.subject_id = database, subject_id
        self.events = EventStore(database)

    @staticmethod
    def event_id(call_id: str) -> str:
        return "evt_epistemic_context_" + call_id

    @staticmethod
    def digest(value: Any) -> bool:
        return (
            isinstance(value, str) and len(value) == 64 and not set(value) - set("0123456789abcdef")
        )

    def calls(self, connection: Any, purpose: str) -> list[Any]:
        return [
            ModelLedger._call_from_row(row)
            for row in connection.execute(
                "SELECT * FROM model_calls WHERE subject_id=? AND purpose=? ORDER BY rowid",
                (self.subject_id, purpose),
            )
        ]

    def blocked(
        self, connection: Any, purpose: str, *, routed: bool, own_call: str | None = None
    ) -> bool:
        sql = UNRUNNABLE_BUSINESS_CALL_SQL.replace(
            ":require_prepared_authorization",
            "(:routed AND (:own IS NULL OR c.call_id!=:own))",
        )
        if connection.execute(
            "SELECT 1 FROM model_calls c WHERE c.subject_id=:subject AND c.purpose=:purpose "
            "AND ("
            + sql
            + " OR (c.status='prepared' AND :own IS NOT NULL AND c.call_id!=:own)) LIMIT 1",
            {
                "subject": self.subject_id,
                "purpose": purpose,
                "routed": int(routed),
                "own": own_call,
            },
        ).fetchone():
            return True
        return (
            connection.execute(
                "SELECT 1 FROM model_attempts a JOIN model_calls c ON c.call_id=a.call_id "
                "WHERE c.subject_id=? AND c.purpose=? "
                "AND a.status IN ('authorized','executing') LIMIT 1",
                (self.subject_id, purpose),
            ).fetchone()
            is not None
        )

    @staticmethod
    def request_matches(call: Any, context: Any, settings: Any) -> bool:
        from .epistemic import EpistemicReview, EpistemicReviewProposal

        efforts: tuple[Literal["low", "medium", "high", "xhigh"] | None, ...] = (
            None,
            "low",
            "medium",
            "high",
            "xhigh",
        )
        return call.request_hash in {
            content_hash(
                CompletionRequest(
                    model=call.model,
                    messages=EpistemicReview._messages(context),
                    max_output_tokens=min(4000, settings.max_output_tokens),
                    temperature=settings.temperature,
                    schema_name=EpistemicReviewProposal.__name__,
                    output_schema=EpistemicReviewProposal.model_json_schema(),
                    reasoning_effort=effort,
                ).model_dump(mode="json")
            )
            for effort in efforts
        }

    def observation(self, connection: Any, observation_id: str) -> tuple[Any, dict[str, Any]]:
        row = connection.execute(
            "SELECT * FROM observations WHERE subject_id=? AND observation_id=?",
            (self.subject_id, observation_id),
        ).fetchone()
        if row is None:
            raise EpistemicContextUnavailable("original observation is unavailable")
        record = ObservationStore._from_row(row)
        if record.status != "analyzed":
            raise EpistemicContextUnavailable("original observation is no longer analyzed")
        event = connection.execute(
            "SELECT * FROM events WHERE subject_id=? AND event_id=?",
            (self.subject_id, record.event_id),
        ).fetchone()
        if event is None:
            raise IntegrityError("epistemic observation event is missing")
        verified = self.events._from_row(event)
        try:
            assert_event_sources_available(connection, self.subject_id, record.event_id)
        except PermissionError as error:
            raise EpistemicContextUnavailable("original observation source withdrawn") from error
        return record, {
            "event_id": record.event_id,
            "event_hash": verified.payload_hash,
            "fingerprint": content_hash(asdict(record)),
        }

    def state_row(self, connection: Any, kind: str, identifier: str) -> dict[str, Any]:
        record: Any
        if kind == "beliefs":
            record = BeliefStore._load_connection(connection, identifier)
            fields, table, key = BELIEF_FIELDS, "beliefs", "belief_id"
        else:
            record = PredictionStore._load_connection(connection, identifier)
            fields, table, key = PREDICTION_FIELDS, "predictions", "prediction_id"
        if record.subject_id != self.subject_id:
            raise IntegrityError("epistemic input belongs to another subject")
        return dict(
            connection.execute(
                f"SELECT {fields} FROM {table} WHERE {key}=? AND subject_id=?",
                (identifier, self.subject_id),
            ).fetchone()
        )

    def capture(self, connection: Any, call: Any, context: Any) -> dict[str, Any]:
        data = strict_json_loads(context.serialized)
        if (
            data["trigger_observation_id"] != context.trigger_observation_id
            or data["beliefs"] != list(context.belief_rows.values())
            or data["due_predictions"] != list(context.prediction_rows.values())
        ):
            raise IntegrityError("epistemic prompt and allowed targets differ")
        observations = {}
        for item in data["analyzed_observations"]:
            observation, descriptor = self.observation(connection, item["observation_id"])
            expected = {
                "observation_id": observation.observation_id,
                "title": observation.title,
                "content": observation.content[:4000],
                "fetched_at": observation.fetched_at,
            }
            if (
                item != expected
                or context.eligible_observations.get(observation.observation_id)
                != observation.event_id
            ):
                raise EpistemicContextUnavailable(
                    "epistemic observation changed before preparation"
                )
            observations[observation.observation_id] = descriptor
        if set(observations) != set(context.eligible_observations):
            raise IntegrityError("epistemic allowed observation set differs")
        states: dict[str, dict[str, Any]] = {}
        for kind, rows in (
            ("beliefs", context.belief_rows),
            ("predictions", context.prediction_rows),
        ):
            states[kind] = {}
            for identifier, row in rows.items():
                if self.state_row(connection, kind, identifier) != row:
                    raise EpistemicContextUnavailable("epistemic target changed before preparation")
                states[kind][identifier] = content_hash(row)
        _, trigger = self.observation(connection, context.trigger_observation_id)
        return {
            "format": FORMAT,
            "subject_id": self.subject_id,
            "call_id": call.call_id,
            "purpose": call.purpose,
            "request_hash": call.request_hash,
            "context_hash": content_hash(data),
            "trigger_observation_id": context.trigger_observation_id,
            "trigger": trigger,
            "observations": observations,
            **states,
        }

    def bind(
        self, connection: Any, call: Any, context: Any, settings: Any, *, routed: bool
    ) -> None:
        if (
            call.subject_id != self.subject_id
            or call.purpose != f"epistemic_review:{context.trigger_observation_id}"
        ):
            raise IntegrityError("epistemic preparation belongs to another round")
        if self.blocked(connection, call.purpose, routed=routed, own_call=call.call_id):
            raise EpistemicContextUnavailable("epistemic request still needs reconciliation")
        if any(
            previous.call_id != call.call_id and previous.status == "succeeded"
            for previous in self.calls(connection, call.purpose)
        ):
            raise EpistemicContextUnavailable("a paid epistemic result appeared before preparation")
        if not self.request_matches(call, context, settings):
            raise IntegrityError("epistemic request differs from original input")
        payload = self.capture(connection, call, context)
        previous = self.read(connection, call)
        if previous is not None:
            if previous != payload:
                raise EpistemicContextUnavailable("prepared epistemic input changed")
            return
        self.events._append_connection(
            connection,
            self.subject_id,
            "epistemic_context_bound",
            "epistemic_supervisor",
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
        event = self.events._from_row(row)
        value = event.payload
        if (
            event.subject_id != self.subject_id
            or event.event_type != "epistemic_context_bound"
            or event.source != "epistemic_supervisor"
            or event.privacy_level != "private"
            or set(value)
            != {
                "format",
                "subject_id",
                "call_id",
                "purpose",
                "request_hash",
                "context_hash",
                "trigger_observation_id",
                "trigger",
                "observations",
                "beliefs",
                "predictions",
            }
            or value["format"] != FORMAT
            or value["subject_id"] != self.subject_id
            or call.subject_id != self.subject_id
            or value["call_id"] != call.call_id
            or value["purpose"] != call.purpose
            or value["request_hash"] != call.request_hash
            or not self.digest(value["context_hash"])
            or not isinstance(value["trigger_observation_id"], str)
            or call.purpose != f"epistemic_review:{value['trigger_observation_id']}"
        ):
            raise IntegrityError("epistemic input binding is invalid")
        for kind, maximum in (("observations", 24), ("beliefs", 16), ("predictions", 16)):
            entries = value[kind]
            if (
                not isinstance(entries, dict)
                or len(entries) > maximum
                or any(not isinstance(key, str) or not key or len(key) > 256 for key in entries)
            ):
                raise IntegrityError("epistemic input identities are invalid")
            for descriptor in entries.values():
                if kind == "observations":
                    self.check_descriptor(descriptor)
                elif not self.digest(descriptor):
                    raise IntegrityError("epistemic target fingerprint is invalid")
        self.check_descriptor(value["trigger"])
        return value

    def check_descriptor(self, descriptor: Any) -> None:
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != {"event_id", "event_hash", "fingerprint"}
            or not isinstance(descriptor["event_id"], str)
            or not descriptor["event_id"]
            or not self.digest(descriptor["event_hash"])
            or not self.digest(descriptor["fingerprint"])
        ):
            raise IntegrityError("epistemic observation binding is invalid")

    def recover(self, connection: Any, call: Any, current: Any, settings: Any) -> Any:
        from .epistemic import _ReviewContext

        bound = self.read(connection, call)
        if bound is None:
            if not self.request_matches(call, current, settings):
                raise EpistemicContextUnavailable("legacy epistemic input cannot be proven")
            # An exact request-hash match proves the current input; do not invent
            # an immutable pre-upgrade receipt or any missing historical text.
            bound = self.capture(connection, call, current)
        eligible = {}
        _, trigger = self.observation(connection, bound["trigger_observation_id"])
        if trigger != bound["trigger"]:
            raise EpistemicContextUnavailable("epistemic trigger changed")
        for identifier, descriptor in bound["observations"].items():
            observation, actual = self.observation(connection, identifier)
            if actual != descriptor:
                raise EpistemicContextUnavailable("epistemic original evidence changed")
            eligible[identifier] = observation.event_id
        states: dict[str, dict[str, Any]] = {"beliefs": {}, "predictions": {}}
        for kind in states:
            for identifier in bound[kind]:
                try:
                    states[kind][identifier] = self.state_row(connection, kind, identifier)
                except NotFoundError:
                    continue  # Validation rejects any proposal targeting it.
        return _ReviewContext(
            "{}",
            bound["trigger_observation_id"],
            eligible,
            states["beliefs"],
            states["predictions"],
            bound,
        )

    def validate_targets(self, connection: Any, context: Any, proposal: Any) -> None:
        from .epistemic import EpistemicReviewValidationError

        bound = context.binding
        if bound is None:
            raise IntegrityError("epistemic commit has no verified input")
        for kind, items, key in (
            ("beliefs", proposal.belief_revisions, "belief_id"),
            ("predictions", proposal.prediction_resolutions, "prediction_id"),
        ):
            for item in items:
                identifier = getattr(item, key)
                try:
                    row = (
                        self.state_row(connection, kind, identifier)
                        if identifier in bound[kind]
                        else None
                    )
                except NotFoundError:
                    row = None
                if row is None or content_hash(row) != bound[kind].get(identifier):
                    raise EpistemicReviewValidationError(
                        "epistemic target version changed or unavailable"
                    )

    def check_evidence(self, connection: Any, context: Any) -> None:
        bound = context.binding
        if bound is None:
            raise IntegrityError("epistemic local result has no evidence binding")
        _, trigger = self.observation(connection, context.trigger_observation_id)
        if trigger != bound["trigger"]:
            raise EpistemicContextUnavailable("epistemic trigger changed before commit")
        for identifier, expected in bound["observations"].items():
            _, actual = self.observation(connection, identifier)
            if actual != expected:
                raise EpistemicContextUnavailable("epistemic evidence changed before commit")

    def verify_connection(self, connection: Any) -> int:
        count = 0
        for row in connection.execute(
            "SELECT c.* FROM model_calls c JOIN events e ON e.event_id=?||c.call_id "
            "WHERE c.subject_id=? AND c.purpose LIKE 'epistemic_review:%'",
            ("evt_epistemic_context_", self.subject_id),
        ):
            self.read(connection, ModelLedger._call_from_row(row))
            count += 1
        return count
