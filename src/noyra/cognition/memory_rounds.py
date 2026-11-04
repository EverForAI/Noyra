"""Stable memory-integration rounds and pre-dispatch physical-call evidence.

Only IDs, hashes and event metadata are retained. Original memory text is read
again under its recorded revision; a changed source is never substituted.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.checkpoint_clock import append_checkpoint
from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.events import EventStore
from noyra.core.private_sources import assert_event_sources_available
from noyra.core.types import canonical_json, content_hash
from noyra.mind import MemoryIntegrationError, MemoryStore
from noyra.model import ModelGateway, ModelLedger
from noyra.model.request_identity import UNRUNNABLE_BUSINESS_CALL_SQL, logical_key_from_route

from .types import SemanticMemoryIntegrationProposal

FORMAT = "semantic-memory-round/v1"
EVENT_FIELDS = ("event_id", "event_type", "source", "occurred_at", "payload_hash")


class MemoryIntegrationRounds:
    def __init__(self, database: Database, subject_id: str):
        self.database, self.subject_id = database, subject_id
        self.key = "semantic-memory-rounds:" + content_hash(subject_id)
        self.events = EventStore(database)

    def read(self, connection: Any) -> Any:
        row = connection.execute(
            "SELECT * FROM workflow_checkpoints WHERE workflow_id=? "
            "ORDER BY checkpoint_version DESC LIMIT 1",
            (self.key,),
        ).fetchone()
        if row is None:
            return None
        self.decode(row)
        return row

    def decode(self, row: Any) -> dict[str, Any]:
        record = DurableWorkflowStore._from_row(row)
        state = record.state
        if (
            record.subject_id != self.subject_id
            or record.workflow_type != "semantic_memory_round"
            or record.status != "running"
            or set(state)
            != {
                "format",
                "number",
                "phase",
                "context_hash",
                "sources",
                "events",
                "options",
                "outcome",
                "call_id",
                "integration_id",
            }
            or state.get("format") != FORMAT
            or type(state.get("number")) is not int
            or state["number"] < 0
            or not isinstance(state.get("phase"), str)
            or state.get("phase") not in {"prepared", "closed"}
            or not isinstance(state.get("sources"), list)
            or not 2 <= len(state["sources"]) <= 16
            or not isinstance(state.get("events"), list)
            or not 0 <= len(state["events"]) <= 48
            or not isinstance(state.get("options"), dict)
            or set(state["options"])
            != {"max_output_tokens", "temperature", "schema_hash", "messages_hash"}
            or not self.digest(state.get("context_hash"))
            or any(
                not isinstance(item, dict)
                or set(item) != {"memory_id", "revision", "fingerprint"}
                or type(item["revision"]) is not int
                or item["revision"] < 1
                or not isinstance(item["memory_id"], str)
                or not self.digest(item["fingerprint"])
                for item in state["sources"]
            )
            or len({item["memory_id"] for item in state["sources"]}) != len(state["sources"])
            or type(state["options"].get("max_output_tokens")) is not int
            or not 1 <= state["options"]["max_output_tokens"] <= 3000
            or type(state["options"].get("temperature")) not in {int, float}
            or not 0 <= state["options"]["temperature"] <= 2
            or any(
                not self.digest(state["options"].get(k)) for k in ("schema_hash", "messages_hash")
            )
        ):
            raise IntegrityError("semantic memory round evidence is invalid")
        for event in state["events"]:
            if (
                not isinstance(event, dict)
                or set(event) != set(EVENT_FIELDS)
                or any(not isinstance(v, str) or not v for v in event.values())
            ):
                raise IntegrityError("semantic memory event evidence is invalid")
        if len({e["event_id"] for e in state["events"]}) != len(state["events"]):
            raise IntegrityError("semantic memory event identities are invalid")
        if state["phase"] == "prepared":
            if any(state[k] is not None for k in ("outcome", "call_id", "integration_id")):
                raise IntegrityError("prepared memory round has a terminal outcome")
        elif not isinstance(state["outcome"], str) or state["outcome"] not in {
            "committed",
            "waited",
            "rejected",
            "context_changed",
            "model_failed",
        }:
            raise IntegrityError("semantic memory round outcome is invalid")
        elif state["outcome"] == "committed" and (
            not state["call_id"] or not state["integration_id"]
        ):
            raise IntegrityError("committed memory round is unbound")
        if (
            state["call_id"] is not None
            and (not isinstance(state["call_id"], str) or not state["call_id"])
        ) or (
            state["integration_id"] is not None
            and (not isinstance(state["integration_id"], str) or not state["integration_id"])
        ):
            raise IntegrityError("memory round terminal identity is invalid")
        if (
            state["phase"] == "closed"
            and state["outcome"] != "committed"
            and state["integration_id"] is not None
        ):
            raise IntegrityError("non-committed memory round has an integration result")
        return state

    @staticmethod
    def digest(value: Any) -> bool:
        return (
            isinstance(value, str) and len(value) == 64 and not set(value) - set("0123456789abcdef")
        )

    def state(self, row: Any) -> dict[str, Any]:
        return DurableWorkflowStore._from_row(row).state

    @staticmethod
    def purpose(state: dict[str, Any]) -> str:
        return f"semantic_memory_integration:round:{state['number']}"

    @staticmethod
    def logical_key(state: dict[str, Any]) -> str:
        return f"semantic-memory-integration-round:{state['number']}"

    @staticmethod
    def prepared_hash(state: dict[str, Any]) -> str:
        return content_hash(
            {
                k: state[k]
                for k in ("format", "number", "context_hash", "sources", "events", "options")
            }
        )

    def prepare(
        self,
        connection: Any,
        payload: dict[str, Any],
        memories: list[Any],
        options: dict[str, Any],
        now: str,
    ) -> Any:
        previous = self.read(connection)
        if previous is not None and self.state(previous)["phase"] == "prepared":
            return previous
        identifiers = {m["memory_id"] for m in payload["memories"]}
        state = {
            "format": FORMAT,
            "number": 0 if previous is None else self.state(previous)["number"] + 1,
            "phase": "prepared",
            "context_hash": content_hash(payload),
            "sources": [
                {
                    "memory_id": m.memory_id,
                    "revision": m.current_revision,
                    "fingerprint": content_hash(asdict(m)),
                }
                for m in memories
                if m.memory_id in identifiers
            ],
            "events": payload["events"],
            "options": options,
            "outcome": None,
            "call_id": None,
            "integration_id": None,
        }
        self.append(connection, previous, state, now)
        return self.read(connection)

    def append(self, connection: Any, previous: Any, state: dict[str, Any], now: str) -> Any:
        return append_checkpoint(
            connection,
            self.database,
            subject_id=self.subject_id,
            workflow_id=self.key,
            workflow_type="semantic_memory_round",
            version=1 if previous is None else previous["checkpoint_version"] + 1,
            status="running",
            state=state,
            reason="semantic_memory_round_" + state["phase"],
            observed_at=now,
            previous=previous,
        )

    def reconstruct(self, connection: Any, state: dict[str, Any]) -> dict[str, Any]:
        memories = []
        for item in state["sources"]:
            identifier, fingerprint = item["memory_id"], item["fingerprint"]
            try:
                memory = MemoryStore._load_connection(connection, identifier)
            except NotFoundError as error:
                raise MemoryIntegrationError("memory round source unavailable") from error
            if (
                memory.subject_id != self.subject_id
                or content_hash(asdict(memory)) != fingerprint
                or memory.status != "active"
            ):
                raise MemoryIntegrationError("memory round source changed")
            memories.append(
                {
                    "memory_id": memory.memory_id,
                    "memory_type": memory.memory_type,
                    "content": memory.content[:2000],
                    "confidence": memory.confidence,
                    "salience": memory.salience,
                }
            )
        # Source order is part of the original context and is explicitly persisted.
        for event in state["events"]:
            current = connection.execute(
                "SELECT event_id,event_type,source,occurred_at,payload_hash "
                "FROM events WHERE subject_id=? AND event_id=?",
                (self.subject_id, event["event_id"]),
            ).fetchone()
            if current is None or dict(current) != event:
                raise MemoryIntegrationError("memory round event changed")
            assert_event_sources_available(connection, self.subject_id, event["event_id"])
        payload = {"memories": memories, "events": state["events"]}
        if content_hash(payload) != state["context_hash"]:
            raise IntegrityError("memory round context hash mismatch")
        return payload

    @staticmethod
    def validate_prompt(state: dict[str, Any], payload: dict[str, Any]) -> None:
        from .memory_integration import SemanticMemoryIntegrator

        messages = SemanticMemoryIntegrator._messages(canonical_json(payload))
        if (
            content_hash(SemanticMemoryIntegrationProposal.model_json_schema())
            != state["options"]["schema_hash"]
            or content_hash([m.model_dump(mode="json") for m in messages])
            != state["options"]["messages_hash"]
        ):
            raise MemoryIntegrationError("memory round prompt or schema changed")

    def current(self, connection: Any, state: dict[str, Any]) -> Any:
        current = self.read(connection)
        if (
            current is None
            or self.state(current)["phase"] != "prepared"
            or self.prepared_hash(self.state(current)) != self.prepared_hash(state)
        ):
            raise MemoryIntegrationError("memory round already closed or changed")
        ModelLedger._check_lifecycle(connection, self.subject_id, self.purpose(state))
        return current

    def bind(self, connection: Any, call: Any) -> None:
        current = self.read(connection)
        if current is None:
            raise IntegrityError("memory round is missing before model preparation")
        state = self.state(current)
        self.current(connection, state)
        if (
            call.subject_id != self.subject_id
            or call.purpose != self.purpose(state)
            or logical_key_from_route(call.idempotency_key, call.resource_pool)
            != self.logical_key(state)
        ):
            raise IntegrityError("memory model call belongs to another round")
        payload_context = self.reconstruct(connection, state)
        self.validate_prompt(state, payload_context)
        from noyra.model.types import CompletionRequest

        from .memory_integration import SemanticMemoryIntegrator

        efforts: tuple[Literal["low", "medium", "high", "xhigh"] | None, ...] = (
            None,
            "low",
            "medium",
            "high",
            "xhigh",
        )
        expected = {
            content_hash(
                CompletionRequest(
                    model=call.model,
                    messages=SemanticMemoryIntegrator._messages(canonical_json(payload_context)),
                    max_output_tokens=state["options"]["max_output_tokens"],
                    temperature=state["options"]["temperature"],
                    schema_name=SemanticMemoryIntegrationProposal.__name__,
                    output_schema=SemanticMemoryIntegrationProposal.model_json_schema(),
                    reasoning_effort=effort,
                ).model_dump(mode="json")
            )
            for effort in efforts
        }
        if call.request_hash not in expected:
            raise IntegrityError("memory call request does not match its original context")
        payload = {
            "format": FORMAT,
            "prepared_hash": self.prepared_hash(state),
            "call_id": call.call_id,
            "subject_id": self.subject_id,
            "purpose": call.purpose,
            "request_hash": call.request_hash,
            "idempotency_key": call.idempotency_key,
        }
        self.events._append_connection(
            connection,
            self.subject_id,
            "semantic_memory_call_bound",
            "memory_integration",
            payload,
            privacy_level="private",
            causal_parent_ids=(),
            occurred_at=None,
            event_id="evt_memory_call_bound_" + call.call_id,
        )

    def calls(self, connection: Any, state: dict[str, Any]) -> list[Any]:
        rows = connection.execute(
            "SELECT * FROM model_calls WHERE subject_id=? AND purpose=? ORDER BY rowid LIMIT 257",
            (self.subject_id, self.purpose(state)),
        ).fetchall()
        if len(rows) > 256:
            raise IntegrityError("memory round call count exceeds its bound")
        calls = []
        for row in rows:
            call = ModelLedger._call_from_row(row)
            event = connection.execute(
                "SELECT * FROM events WHERE event_id=?", ("evt_memory_call_bound_" + call.call_id,)
            ).fetchone()
            if event is None:
                raise IntegrityError("memory call has no original context binding")
            payload = self.events.payload_from_row(event, connection=connection)
            expected = {
                "format": FORMAT,
                "prepared_hash": self.prepared_hash(state),
                "call_id": call.call_id,
                "subject_id": self.subject_id,
                "purpose": call.purpose,
                "request_hash": call.request_hash,
                "idempotency_key": call.idempotency_key,
            }
            if (
                event["subject_id"] != self.subject_id
                or event["event_type"] != "semantic_memory_call_bound"
                or event["source"] != "memory_integration"
                or event["privacy_level"] != "private"
                or content_hash(payload) != event["payload_hash"]
                or payload != expected
                or logical_key_from_route(call.idempotency_key, call.resource_pool)
                != self.logical_key(state)
            ):
                raise IntegrityError("memory call context binding is invalid")
            calls.append(call)
        return calls

    def close(
        self,
        connection: Any,
        state: dict[str, Any],
        outcome: str,
        now: str,
        *,
        call_id: str | None = None,
        integration_id: str | None = None,
    ) -> None:
        previous = self.current(connection, state)
        self.append(
            connection,
            previous,
            {
                **state,
                "phase": "closed",
                "outcome": outcome,
                "call_id": call_id,
                "integration_id": integration_id,
            },
            now,
        )

    def verify_integrity(self) -> int:
        with self.database.read_transaction() as connection:
            return self.verify_connection(connection)

    def verify_connection(self, connection: Any) -> int:
        previous: dict[str, Any] | None = None
        count = 0
        for row in connection.execute(
            "SELECT * FROM workflow_checkpoints WHERE workflow_id=? ORDER BY checkpoint_version",
            (self.key,),
        ):
            state = self.decode(row)
            count += 1
            if row["checkpoint_version"] != count:
                raise IntegrityError("memory round checkpoint sequence is invalid")
            calls = self.calls(connection, state)
            if sum(c.status == "succeeded" for c in calls) > 1:
                raise IntegrityError("memory round has multiple successful calls")
            if state["phase"] == "prepared":
                if state["number"] != (0 if previous is None else previous["number"] + 1) or (
                    previous is not None and previous["phase"] != "closed"
                ):
                    raise IntegrityError("memory round sequence is invalid")
            else:
                if (
                    previous is None
                    or previous["phase"] != "prepared"
                    or self.prepared_hash(state) != self.prepared_hash(previous)
                ):
                    raise IntegrityError("memory round terminal context is invalid")
                if any(c.status in {"executing", "unknown"} for c in calls):
                    raise IntegrityError("closed memory round still has an ambiguous model call")
                for call in calls:
                    if (
                        call.status == "prepared"
                        or connection.execute(
                            "SELECT 1 FROM model_calls c WHERE c.call_id=:call AND "
                            + UNRUNNABLE_BUSINESS_CALL_SQL,
                            {"call": call.call_id, "require_prepared_authorization": 1},
                        ).fetchone()
                    ):
                        raise IntegrityError("closed memory round bypassed model reconciliation")
                    if connection.execute(
                        "SELECT 1 FROM model_attempts WHERE call_id=? AND subject_id=? "
                        "AND status IN ('authorized','executing') LIMIT 1",
                        (call.call_id, self.subject_id),
                    ).fetchone():
                        raise IntegrityError("closed memory round still has an unresolved attempt")
                chosen = next((c for c in calls if c.call_id == state["call_id"]), None)
                if (
                    state["outcome"] == "context_changed"
                    and any(c.status == "succeeded" for c in calls)
                    and (chosen is None or chosen.status != "succeeded")
                ):
                    raise IntegrityError(
                        "discarded memory context lost its successful call binding"
                    )
                if state["outcome"] == "model_failed" and (
                    not calls or any(c.status != "failed" for c in calls)
                ):
                    raise IntegrityError("failed memory round has no definite model failure")
                if state["outcome"] in {"committed", "waited", "rejected"}:
                    if chosen is None or chosen.status != "succeeded":
                        raise IntegrityError("memory terminal result is not a bound success")
                    proposal = ModelGateway._cached_result(
                        chosen, SemanticMemoryIntegrationProposal
                    ).output
                    if state["outcome"] == "waited" and proposal.disposition != "wait":
                        raise IntegrityError("memory wait does not match its paid response")
                    if state["outcome"] == "rejected" and proposal.disposition != "integrate":
                        raise IntegrityError("memory rejection does not match its paid response")
                    if state["outcome"] == "committed":
                        from noyra.mind import MemoryIntegrationSupervisor

                        integration = connection.execute(
                            "SELECT * FROM memory_integrations WHERE subject_id=? "
                            "AND integration_id=?",
                            (self.subject_id, state["integration_id"]),
                        ).fetchone()
                        if integration is None:
                            raise IntegrityError("memory round result is missing")
                        record = MemoryIntegrationSupervisor._record(integration)
                        if record.output_memory_id is None:
                            raise IntegrityError("memory round output is missing")
                        output = MemoryStore._load_connection(connection, record.output_memory_id)
                        initial = connection.execute(
                            "SELECT content,content_hash FROM memory_revisions "
                            "WHERE memory_id=? AND revision_number=1",
                            (record.output_memory_id,),
                        ).fetchone()
                        if (
                            initial is None
                            or content_hash(initial["content"]) != initial["content_hash"]
                        ):
                            raise IntegrityError("memory round original output revision is invalid")
                        if (
                            proposal.disposition != "integrate"
                            or record.source_memory_ids != proposal.source_memory_ids
                            or record.evidence_event_ids != proposal.evidence_event_ids
                            or record.operation != proposal.operation
                            or record.reason != proposal.reason.strip()
                            or record.confidence != proposal.confidence
                            or output.subject_id != self.subject_id
                            or initial["content"] != str(proposal.synthesis).strip()
                        ):
                            raise IntegrityError(
                                "memory round integration does not match its response"
                            )
            previous = state
        # Validate issued legacy dispositions even while another legacy object
        # still waits for operator review.
        receipts = connection.execute(
            "SELECT c.* FROM model_calls c JOIN events e ON e.event_id=?||c.call_id "
            "WHERE c.subject_id=?",
            ("evt_memory_legacy_discarded_", self.subject_id),
        )
        for row in receipts:
            self.legacy_receipt(connection, ModelLedger._call_from_row(row))
        return count

    @staticmethod
    def legacy_id(call_id: str) -> str:
        return "evt_memory_legacy_discarded_" + call_id

    def legacy_receipt(self, connection: Any, call: Any) -> bool:
        event = connection.execute(
            "SELECT * FROM events WHERE event_id=?", (self.legacy_id(call.call_id),)
        ).fetchone()
        if event is None:
            return False
        payload = self.events.payload_from_row(event, connection=connection)
        if (
            event["subject_id"] != self.subject_id
            or event["event_type"] != "semantic_memory_legacy_discarded"
            or event["source"] != "operator_control"
            or event["privacy_level"] != "private"
            or content_hash(payload) != event["payload_hash"]
            or set(payload)
            != {"format", "call_id", "call_hash", "actor", "reason", "evidence_sha256"}
            or payload["format"] != FORMAT
            or payload["call_id"] != call.call_id
            or payload["call_hash"] != content_hash(asdict(call))
            or call.status != "succeeded"
            or not call.purpose.startswith("semantic_memory_integration:")
            or call.purpose.startswith("semantic_memory_integration:round:")
            or not isinstance(payload["actor"], str)
            or not payload["actor"].strip()
            or payload["actor"].strip().casefold() == "subject"
            or len(payload["actor"]) > 256
            or not isinstance(payload["reason"], str)
            or not payload["reason"].strip()
            or len(payload["reason"]) > 2000
            or not self.digest(payload["evidence_sha256"])
        ):
            raise IntegrityError("legacy memory disposition evidence is invalid")
        return True

    def legacy_records(self, connection: Any, *, limit: int = 100) -> list[Any]:
        prefix = "semantic_memory_integration:"
        new_prefix = prefix + "round:"
        return list(
            connection.execute(
                "SELECT c.* FROM model_calls c WHERE subject_id=? "
                "AND substr(purpose,1,?)=? AND substr(purpose,1,?)!=? "
                "AND status IN ('prepared','executing','unknown','succeeded') "
                "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.event_id=?||c.call_id) "
                "ORDER BY c.rowid LIMIT ?",
                (
                    self.subject_id,
                    len(prefix),
                    prefix,
                    len(new_prefix),
                    new_prefix,
                    "evt_memory_legacy_discarded_",
                    limit,
                ),
            ).fetchall()
        )

    def legacy_blocked(self, connection: Any) -> bool:
        # A SQL predicate only narrows the set; every candidate receipt is verified.
        prefix = "semantic_memory_integration:"
        new_prefix = prefix + "round:"
        rows = connection.execute(
            "SELECT c.* FROM model_calls c WHERE subject_id=? "
            "AND substr(purpose,1,?)=? AND substr(purpose,1,?)!=? "
            "AND status IN ('prepared','executing','unknown','succeeded') "
            "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.event_id=?||c.call_id) LIMIT 1",
            (
                self.subject_id,
                len(prefix),
                prefix,
                len(new_prefix),
                new_prefix,
                "evt_memory_legacy_discarded_",
            ),
        ).fetchall()
        discarded = connection.execute(
            "SELECT c.* FROM model_calls c JOIN events e ON e.event_id=?||c.call_id "
            "WHERE c.subject_id=?",
            ("evt_memory_legacy_discarded_", self.subject_id),
        )
        for row in discarded:
            self.legacy_receipt(connection, ModelLedger._call_from_row(row))
        return bool(rows)

    def discard_legacy(
        self,
        connection: Any,
        call_id: str,
        fingerprint: str,
        *,
        actor: str,
        reason: str,
        evidence_sha256: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM model_calls WHERE subject_id=? AND call_id=?", (self.subject_id, call_id)
        ).fetchone()
        if row is None:
            raise ValueError("legacy_memory_call_not_found")
        call = ModelLedger._call_from_row(row)
        prefix = "semantic_memory_integration:"
        if (
            call.status != "succeeded"
            or not call.purpose.startswith(prefix)
            or call.purpose.startswith(prefix + "round:")
            or content_hash(asdict(call)) != fingerprint
            or not self.digest(evidence_sha256)
        ):
            raise ValueError("legacy_memory_snapshot_or_status_conflict")
        if self.legacy_receipt(connection, call):
            return {"call_id": call_id, "idempotent": True}
        self.events._append_connection(
            connection,
            self.subject_id,
            "semantic_memory_legacy_discarded",
            "operator_control",
            {
                "format": FORMAT,
                "call_id": call_id,
                "call_hash": fingerprint,
                "actor": actor,
                "reason": reason,
                "evidence_sha256": evidence_sha256,
            },
            privacy_level="private",
            causal_parent_ids=(),
            occurred_at=None,
            event_id=self.legacy_id(call_id),
        )
        return {"call_id": call_id, "idempotent": False}
