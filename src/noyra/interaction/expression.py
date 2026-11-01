"""Subject-owned writing and fixed-route outbox using existing durable ledgers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from noyra.capability import CapabilityGrant, CapabilityStore
from noyra.capability.types import CapabilityType
from noyra.core.actions import ActionLedger
from noyra.core.admission import accounting_scope, assert_current_lease
from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.redaction import redact_secret_text
from noyra.core.types import ActionRecord, content_hash, utc_now
from noyra.model import ModelLedger

from .public_archive import PublicArchive
from .store import InteractionStore


class ExpressionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    operation: Literal["write", "revise", "withdraw", "message", "wait"]
    title: str = Field(default="", max_length=240)
    summary: str = Field(default="", max_length=1_000)
    body: str = Field(default="", max_length=20_000)
    publication_id: str | None = Field(default=None, max_length=100)
    expected_revision: int = Field(default=0, ge=0)
    transport_id: str | None = Field(default=None, max_length=256)
    recipient: str | None = Field(default=None, max_length=512)
    observation_ids: tuple[str, ...] = Field(default=(), max_length=8)
    reason: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def shape(self) -> ExpressionProposal:
        if not self.reason.strip():
            raise ValueError("expression reason is blank")
        if self.operation in {"write", "revise"} and not all(
            value.strip() for value in (self.title, self.summary, self.body)
        ):
            raise ValueError("article requires title summary and body")
        if self.operation == "message" and (
            not self.body.strip() or not self.transport_id or not self.recipient
        ):
            raise ValueError("message requires a fixed transport recipient and content")
        if self.operation in {"revise", "withdraw"} and (
            not self.publication_id or self.expected_revision < 1
        ):
            raise ValueError("revision requires an owned publication version")
        if self.operation not in {"revise", "withdraw"} and self.publication_id is not None:
            raise ValueError("new expression cannot target an existing publication")
        if self.operation != "message" and (
            self.transport_id is not None or self.recipient is not None
        ):
            raise ValueError("non-message cannot have a recipient")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("expression evidence must be distinct")
        return self


class ExpressionReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    approved: bool
    reason: str = Field(min_length=1, max_length=2_000)


class ExpressionStore:
    def __init__(self, database: Database, subject_id: str):
        self.database, self.subject_id = database, subject_id
        self.actions = ActionLedger(database)
        self.capabilities = CapabilityStore(database)
        self.events = EventStore(database)
        self.archive = PublicArchive(database)

    def ensure_grants(self, *, enabled: bool, rate: int) -> None:
        """Environment-managed sustained consent; an explicit revoke is durable."""
        targets: list[tuple[CapabilityType, str]] = [("publish", "instance:" + self.subject_id)]
        with self.database.connection() as connection:
            transports = connection.execute(
                "SELECT transport_id,config_json FROM interaction_transports "
                "WHERE subject_id=? AND status='active'",
                (self.subject_id,),
            ).fetchall()
        from .transport import TransportStore

        for row in transports:
            config = TransportStore._config(row)
            if config.get("autonomous_send", True) is True:
                targets.append(("message", "transport:" + str(row["transport_id"])))
        grants = self.capabilities.list(self.subject_id)
        if not enabled:
            for grant in grants:
                if grant.issuer == "environment-expression" and grant.status == "active":
                    self.capabilities.revoke(
                        grant.grant_id,
                        reason="expression disabled by configuration",
                        actor="environment-operator",
                        subject_id=self.subject_id,
                    )
            return
        for kind, target in targets:
            managed = [
                grant
                for grant in grants
                if grant.capability_type == kind
                and grant.scope == {"exact": [target]}
                and grant.issuer == "environment-expression"
            ]
            automatic = {"expression disabled by configuration", "expression limits changed"}
            if any(
                grant.status == "revoked" and grant.revoke_reason not in automatic
                for grant in managed
            ):
                continue
            if any(
                grant.status == "active" and grant.rate_limit_per_hour == rate for grant in managed
            ):
                continue
            for grant in managed:
                if grant.status == "active":
                    self.capabilities.revoke(
                        grant.grant_id,
                        reason="expression limits changed",
                        actor="environment-operator",
                        subject_id=self.subject_id,
                    )
            proposal = CapabilityGrant(
                capability_type=kind,
                scope={"exact": [target]},
                issuer="environment-expression",
                rate_limit_per_hour=rate,
                side_effect=True,
            )
            self.capabilities.grant(self.subject_id, proposal, actor="environment-operator")

    @staticmethod
    def validate_text(proposal: ExpressionProposal) -> None:
        for value in (proposal.title, proposal.summary, proposal.body):
            if redact_secret_text(value) != value:
                raise ValueError("expression contains credential material")
        if proposal.recipient is not None and (
            not proposal.recipient.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in proposal.recipient)
        ):
            raise ValueError("invalid expression recipient")

    def prepare(self, proposal: ExpressionProposal, planner_call_id: str) -> ActionRecord:
        self.validate_text(proposal)
        if proposal.operation == "wait":
            raise ValueError("a wait has no external action")
        target = (
            "transport:" + str(proposal.transport_id)
            if proposal.operation == "message"
            else "instance:" + self.subject_id
        )
        return self.actions.prepare(
            self.subject_id,
            "expression",
            "autonomous_expression",
            target,
            proposal.model_dump(mode="json"),
            side_effect=True,
            idempotency_key="expression:" + planner_call_id,
        )

    def execute(
        self,
        action_id: str,
        proposal: ExpressionProposal,
        *,
        review_call_id: str,
        before_commit: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        self.validate_text(proposal)
        review = ModelLedger(self.database).get_call(review_call_id)
        if (
            review.subject_id != self.subject_id
            or review.status != "succeeded"
            or review.purpose != "expression_review:" + action_id
        ):
            raise PermissionError("expression self-review is unavailable")
        output = ExpressionReview.model_validate_json(
            str((review.response or {}).get("content", ""))
        )
        if not output.approved:
            raise PermissionError("expression was not approved by its self-review")
        with self.database.connection() as connection:
            action = ActionLedger._load_connection(connection, action_id)
        if (
            action.subject_id != self.subject_id
            or action.tool != "autonomous_expression"
            or action.input_hash != content_hash(proposal.model_dump(mode="json"))
        ):
            raise IntegrityError("expression draft action mismatch")
        if action.status == "succeeded":
            return action.result or {}
        if action.status != "prepared":
            return {"status": action.status, "action_id": action_id}
        kind = "message" if proposal.operation == "message" else "publish"
        with self.database.connection() as connection:
            charged = connection.execute(
                "SELECT grant_id FROM capability_uses WHERE subject_id=? "
                "AND action_id=? AND resource=? ORDER BY created_at LIMIT 1",
                (self.subject_id, action_id, action.target),
            ).fetchone()
        if charged is None:
            grant = self.capabilities.use(
                self.subject_id, kind, action.target, side_effect=True, action_id=action_id
            )
        else:
            grant = self.capabilities.revalidate_use(
                str(charged[0]), self.subject_id, kind, action.target, side_effect=True
            )
        self.actions.start(action_id)

        def authorize() -> None:
            assert_current_lease()
            self.capabilities.revalidate_use(
                grant.grant_id, self.subject_id, kind, action.target, side_effect=True
            )
            if before_commit is not None:
                before_commit()
            assert_current_lease()

        # The local effect and terminal action receipt share one transaction.
        # External network delivery happens later through DeliveryDispatcher.
        try:
            with self.database.transaction() as connection:
                authorize()
                if proposal.observation_ids:
                    from noyra.world import ObservationStore

                    for observation_id in proposal.observation_ids:
                        evidence = ObservationStore(self.database).get(
                            observation_id, subject_id=self.subject_id
                        )
                        if evidence.injection_signals:
                            raise PermissionError(
                                "expression evidence requires untrusted-data handling"
                            )
                identifier = proposal.publication_id or "expression_" + content_hash(action_id)[:48]
                if proposal.operation == "message":
                    self._transport(connection, proposal)
                    interaction = InteractionStore(self.database)._create_connection(
                        connection,
                        self.subject_id,
                        "outgoing",
                        "subject_message",
                        self._channel(connection, proposal),
                        str(proposal.recipient),
                        proposal.body,
                        None,
                        "expression-message:" + action_id,
                        "sent",
                        "subject self-reviewed expression",
                    )
                    event_id = "evt_outbox_" + content_hash(interaction.interaction_id)
                    self.events._append_connection(
                        connection,
                        self.subject_id,
                        "expression_outbox",
                        "subject",
                        {
                            "interaction_id": interaction.interaction_id,
                            "transport_id": proposal.transport_id,
                            "recipient": proposal.recipient,
                            "grant_id": grant.grant_id,
                            "action_id": action_id,
                        },
                        privacy_level="private",
                        causal_parent_ids=(),
                        occurred_at=utc_now(),
                        event_id=event_id,
                    )
                    receipt = {
                        "status": "queued",
                        "interaction_id": interaction.interaction_id,
                        "transport_id": proposal.transport_id,
                        "action_id": action_id,
                    }
                elif proposal.operation == "withdraw":
                    self.archive.withdraw(
                        self.subject_id,
                        identifier,
                        proposal.expected_revision,
                        actor="subject",
                        _connection=connection,
                        _subject_action=action_id,
                        _authorize=authorize,
                    )
                    receipt = {
                        "status": "withdrawn",
                        "publication_id": identifier,
                        "action_id": action_id,
                    }
                else:
                    item = self.archive.publish(
                        self.subject_id,
                        {
                            "publication_id": identifier,
                            "kind": "update",
                            "title": proposal.title,
                            "summary": proposal.summary,
                            "body": proposal.body,
                            "details": {
                                "status": "subject_help"
                                if action.idempotency_key.startswith("expression:help:")
                                else "subject_article"
                            },
                            "expected_revision": proposal.expected_revision,
                            "confirm_public": True,
                        },
                        actor="subject",
                        _connection=connection,
                        _subject_action=action_id,
                        _authorize=authorize,
                    )
                    receipt = {
                        "status": "published",
                        "publication_id": identifier,
                        "revision": item["revision"],
                        "action_id": action_id,
                    }
                self.actions.finish(
                    action_id,
                    "succeeded",
                    receipt,
                    _connection=connection,
                    public_target="instance" if kind == "publish" else "[private-recipient]",
                    public_explanation="Self-reviewed expression published."
                    if kind == "publish"
                    else "Self-reviewed message queued; delivery is tracked separately.",
                    side_effect_summary=str(receipt["status"]),
                )
                return receipt
        except Exception:
            # This executor performs local SQLite effects only. Rollback is
            # certain, so a known failure is not left as a phantom sending job.
            with accounting_scope():
                self.actions.finish(
                    action_id,
                    "failed",
                    {"error": "expression_commit_failed"},
                    public_target="[private]",
                    public_explanation="Expression stopped before publication.",
                )
            raise

    def _transport(self, connection: Any, proposal: ExpressionProposal) -> Any:
        from .transport import TransportStore

        row = connection.execute(
            "SELECT * FROM interaction_transports WHERE subject_id=? "
            "AND transport_id=? AND status='active'",
            (self.subject_id, proposal.transport_id),
        ).fetchone()
        if row is None:
            raise PermissionError("expression transport unavailable")
        config = TransportStore._config(row)
        if config.get("autonomous_send", True) is not True:
            raise PermissionError("autonomous sending disabled for transport")
        allowed = config.get("allowed_recipients")
        if (
            isinstance(allowed, str)
            and allowed.strip()
            and proposal.recipient not in {value.strip() for value in allowed.split(",")}
        ):
            raise PermissionError("recipient outside optional operator scope")
        return row

    def _channel(self, connection: Any, proposal: ExpressionProposal) -> str:
        return str(self._transport(connection, proposal)["channel"])
