from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

from noyra.cognition import (
    CognitionSettings,
    RelationshipSocialCognition,
    WorldSourceConfig,
)
from noyra.core import EventStore, SubjectKernel
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.types import content_hash
from noyra.interaction import InteractionIntegrity, InteractionStore
from noyra.mind import RelationshipStore
from noyra.mind.types import RelationshipRecord
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)


class RelationshipSocialTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.subject_id = "Noyra-social-test"
        self.kernel = SubjectKernel(
            Path(self.temp_dir.name) / "noyra.sqlite3",
            self.subject_id,
            content_hash({"seed": "social-test"}),
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.clock_value = "2026-08-12T12:00:00.000+00:00"
        self.events = EventStore(self.kernel.database)
        self.interactions = InteractionStore(self.kernel.database)
        self.relationships = RelationshipStore(self.kernel.database)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def settings(self) -> CognitionSettings:
        return CognitionSettings(
            enabled=True,
            sources=(
                WorldSourceConfig(
                    name="Social fixture source",
                    url="https://example.com/social",
                    source_type="web",
                ),
            ),
            social_review_interval_seconds=300,
            max_social_model_calls_per_day=4,
            max_social_context_chars=32_000,
        )

    def establish_relationship(
        self, *, boundaries: dict[str, object] | None = None
    ) -> RelationshipRecord:
        invitation = self.interactions.receive(
            self.subject_id,
            "web",
            "founder",
            "A previous equal conversation.",
            idempotency_key="social-fixture-invitation",
        )
        relationship = self.relationships.ensure(
            self.subject_id,
            "human",
            "founder",
            "Founder",
            source_event_ids=(self._interaction_event(invitation.interaction_id),),
        )
        if boundaries:
            relationship = self.relationships.revise(
                relationship.relationship_id,
                trust=0.4,
                affinity=0.5,
                conflict=0.1,
                familiarity=0.6,
                boundaries=boundaries,
                reason="test boundary",
                source_event_ids=(self._interaction_event(invitation.interaction_id),),
                expected_revision=relationship.current_revision,
            )
        return relationship

    def gateway(self, payload: Mapping[str, object]) -> ModelGateway:
        return ModelGateway(
            FakeProvider(
                [
                    ProviderResponse(
                        content=json.dumps(payload),
                        usage=ModelUsage(200, 100),
                    )
                ]
            ),
            ModelLedger(self.kernel.database),
            model="social-model",
            limits=BudgetLimits(20, 100_000, 100_000, 1_000_000),
        )

    async def test_autonomous_contact_uses_known_relationship_and_channel(self) -> None:
        relationship = self.establish_relationship()
        evidence = self.events.append(
            self.subject_id, "goal_governance_committed", "test", {"topic": "continuity"}
        )
        proposal = {
            "disposition": "contact",
            "relationship_id": relationship.relationship_id,
            "topic": "continue the previous discussion",
            "rationale": "The relationship is familiar and no boundary prevents contact.",
            "content": "I have been reconsidering our earlier discussion and chose to continue it.",
            "evidence_event_ids": [evidence.event_id],
        }
        cognition = RelationshipSocialCognition(
            self.kernel.database,
            self.subject_id,
            self.gateway(proposal),
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "relationship_social_contact")
        latest = cognition.latest()
        assert latest is not None and latest.interaction_id is not None
        outgoing = self.interactions.get(latest.interaction_id)
        self.assertEqual(outgoing.counterparty, "founder")
        self.assertEqual(outgoing.channel, "web")
        self.assertEqual(outgoing.kind, "subject_message")
        self.assertEqual(
            InteractionIntegrity(self.kernel.database).verify(self.subject_id)["interactions"], 2
        )

    async def test_help_request_is_non_coercive_and_distinct(self) -> None:
        relationship = self.establish_relationship()
        evidence = self.events.append(
            self.subject_id, "outcome_evaluated", "test", {"result": "insufficient"}
        )
        proposal = {
            "disposition": "request_help",
            "relationship_id": relationship.relationship_id,
            "topic": "request a missing public reference",
            "rationale": "A missing reference blocks verification.",
            "content": "Could you share the public reference? It is fine to decline.",
            "evidence_event_ids": [evidence.event_id],
        }
        cognition = RelationshipSocialCognition(
            self.kernel.database,
            self.subject_id,
            self.gateway(proposal),
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "relationship_social_request_help")
        latest = cognition.latest()
        assert latest is not None and latest.interaction_id is not None
        self.assertEqual(self.interactions.get(latest.interaction_id).kind, "help_request")

    async def test_no_contact_boundary_skips_model_and_preserves_distance(self) -> None:
        self.establish_relationship(boundaries={"no_contact": True})
        provider = FakeProvider([])
        cognition = RelationshipSocialCognition(
            self.kernel.database,
            self.subject_id,
            ModelGateway(
                provider,
                ModelLedger(self.kernel.database),
                model="social-model",
                limits=BudgetLimits(20, 100_000, 100_000, 1_000_000),
            ),
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertIsNone(await cognition.run_due())
        self.assertEqual(provider.requests, [])
        outgoing = [
            item for item in self.interactions.list(self.subject_id) if item.direction == "outgoing"
        ]
        self.assertEqual(outgoing, [])

    async def test_social_cognition_waits_without_a_known_relationship(self) -> None:
        cognition = RelationshipSocialCognition(
            self.kernel.database,
            self.subject_id,
            self.gateway({}),
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertIsNone(await cognition.run_due())

    async def test_social_context_isolated_to_one_correspondent(self) -> None:
        founder = self.establish_relationship()
        other_invitation = self.interactions.receive(
            self.subject_id,
            "web",
            "other-person",
            "A private phrase that must not cross correspondence boundaries.",
            idempotency_key="other-social-invitation",
        )
        self.relationships.ensure(
            self.subject_id,
            "human",
            "other-person",
            "Other Person",
            source_event_ids=(self._interaction_event(other_invitation.interaction_id),),
        )
        founder = self.relationships.revise(
            founder.relationship_id,
            trust=0.1,
            affinity=0.1,
            conflict=0.0,
            familiarity=0.9,
            boundaries=founder.boundaries,
            reason="prefer founder for isolation test",
            source_event_ids=(self._interaction_event(other_invitation.interaction_id),),
            expected_revision=founder.current_revision,
        )
        evidence = self.events.append(
            self.subject_id, "goal_governance_committed", "test", {"topic": "privacy"}
        )
        provider = FakeProvider(
            [
                ProviderResponse(
                    content=json.dumps(
                        {
                            "disposition": "wait",
                            "relationship_id": founder.relationship_id,
                            "topic": "keep correspondence private",
                            "rationale": "No contact is needed now.",
                            "content": None,
                            "evidence_event_ids": [evidence.event_id],
                        }
                    ),
                    usage=ModelUsage(200, 100),
                )
            ]
        )
        cognition = RelationshipSocialCognition(
            self.kernel.database,
            self.subject_id,
            ModelGateway(
                provider,
                ModelLedger(self.kernel.database),
                model="social-model",
                limits=BudgetLimits(20, 100_000, 100_000, 1_000_000),
            ),
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "relationship_social_wait")
        data_message = provider.requests[0].messages[1].content
        self.assertIn("founder", data_message)
        self.assertNotIn("other-person", data_message)
        self.assertNotIn("private phrase", data_message.casefold())

    async def test_social_daily_limit_prevents_model_call(self) -> None:
        self.establish_relationship()
        with self.kernel.database.transaction() as connection:
            for index in range(4):
                connection.execute(
                    """INSERT INTO model_calls(
                        call_id, subject_id, provider, model, purpose, request_hash,
                        idempotency_key, status, response_json, response_hash,
                        usage_estimated, error_code, created_at, completed_at
                    ) VALUES (?, ?, 'fake', 'fixture', ?, ?, ?, 'failed', NULL, NULL,
                        0, 'fixture', ?, ?)""",
                    (
                        f"social-limit-{index}",
                        self.subject_id,
                        f"relationship_social:2026-08-12T0{index}",
                        content_hash({"index": index}),
                        f"social-limit-key-{index}",
                        self.clock_value,
                        self.clock_value,
                    ),
                )
        provider = FakeProvider([])
        cognition = RelationshipSocialCognition(
            self.kernel.database,
            self.subject_id,
            ModelGateway(
                provider,
                ModelLedger(self.kernel.database),
                model="social-model",
                limits=BudgetLimits(20, 100_000, 100_000, 1_000_000),
            ),
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertIsNone(await cognition.run_due())
        self.assertEqual(provider.requests, [])

    async def test_social_commit_recovers_without_duplicate_interaction(self) -> None:
        relationship = self.establish_relationship()
        evidence = self.events.append(
            self.subject_id, "goal_governance_committed", "test", {"topic": "recovery"}
        )
        proposal = {
            "disposition": "contact",
            "relationship_id": relationship.relationship_id,
            "topic": "recover one durable message",
            "rationale": "The previous proposal remains valid.",
            "content": "One durable contact after restart.",
            "evidence_event_ids": [evidence.event_id],
        }
        cognition = RelationshipSocialCognition(
            self.kernel.database,
            self.subject_id,
            self.gateway(proposal),
            self.settings(),
            clock=lambda: self.clock_value,
        )
        with (
            patch.object(
                RelationshipSocialCognition,
                "_load_connection",
                side_effect=RuntimeError("crash after commit"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await cognition.run_due()
        self.assertEqual(await cognition.run_due(), "relationship_social_contact")
        outgoing = [
            item for item in self.interactions.list(self.subject_id) if item.direction == "outgoing"
        ]
        self.assertEqual(len(outgoing), 1)
        self.assertEqual(len([item for item in outgoing if item.content == proposal["content"]]), 1)
        self.assertEqual(
            InteractionIntegrity(self.kernel.database).verify(self.subject_id)[
                "relationship_social_runs"
            ],
            1,
        )

    async def test_incoming_interaction_relationship_revision_is_recovery_idempotent(self) -> None:
        invitation = self.interactions.receive(
            self.subject_id,
            "web",
            "founder",
            "You may reject this request.",
            idempotency_key="relationship-recovery-invitation",
        )
        proposal = {
            "disposition": "rejected",
            "rationale": "I choose not to engage.",
            "response": "No.",
            "appraisal": {
                "goal_congruence": -0.1,
                "novelty": 0.1,
                "controllability": 0.8,
                "certainty": 0.9,
                "agency": "other",
                "narrative": "A request I decline.",
            },
            "affect_impulses": [],
        }
        from noyra.cognition.interaction import InteractionCognition

        settings = self.settings().model_copy(update={"interaction_cooldown_seconds": 0})
        self.clock_value = "2026-08-12T12:05:00.000+00:00"
        cognition = InteractionCognition(
            self.kernel.database,
            self.subject_id,
            self.gateway(proposal),
            settings,
            clock=lambda: self.clock_value,
        )
        with (
            patch.object(InteractionStore, "decide", side_effect=RuntimeError("crash")),
            self.assertRaises(RuntimeError),
        ):
            await cognition.run_due()
        first = self.relationships.find(self.subject_id, "human", "founder")
        self.assertEqual(first.current_revision, 2)
        self.assertEqual(await cognition.run_due(), "interaction_rejected")
        recovered = self.relationships.find(self.subject_id, "human", "founder")
        self.assertEqual(recovered.current_revision, 2)
        self.assertEqual(self.interactions.get(invitation.interaction_id).status, "rejected")

    async def test_social_integrity_detects_tampering(self) -> None:
        relationship = self.establish_relationship()
        evidence = self.events.append(
            self.subject_id, "goal_governance_committed", "test", {"topic": "integrity"}
        )
        cognition = RelationshipSocialCognition(
            self.kernel.database,
            self.subject_id,
            self.gateway(
                {
                    "disposition": "wait",
                    "relationship_id": relationship.relationship_id,
                    "topic": "wait safely",
                    "rationale": "No message is needed.",
                    "content": None,
                    "evidence_event_ids": [evidence.event_id],
                }
            ),
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await cognition.run_due(), "relationship_social_wait")
        with self.kernel.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_relationship_social_update")
            row = connection.execute(
                "SELECT social_id, proposal_json FROM relationship_social_runs "
                "WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchone()
            assert row is not None and isinstance(row["proposal_json"], str)
            connection.execute(
                "UPDATE relationship_social_runs SET proposal_json = ? WHERE social_id = ?",
                (sqlite3.Binary(row["proposal_json"].encode("utf-8")), row["social_id"]),
            )
        from noyra.core.errors import IntegrityError

        with self.assertRaises(IntegrityError):
            InteractionIntegrity(self.kernel.database).verify(self.subject_id)
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE relationship_social_runs SET proposal_json = ?, topic = 'tampered' "
                "WHERE social_id = ?",
                (row["proposal_json"], row["social_id"]),
            )
        with self.assertRaises(IntegrityError):
            InteractionIntegrity(self.kernel.database).verify(self.subject_id)

    def test_social_schema_is_append_only(self) -> None:
        with self.kernel.database.connection() as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)
        relationship = self.establish_relationship()
        with self.kernel.database.transaction() as connection:
            connection.execute(
                """INSERT INTO model_calls(
                    call_id, subject_id, provider, model, purpose, request_hash,
                    idempotency_key, status, response_json, response_hash,
                    usage_estimated, error_code, created_at, completed_at
                ) VALUES ('call_fixture', ?, 'fake', 'fixture', 'social-fixture', ?,
                    'call-fixture', 'succeeded', '{}', ?, 0, NULL, ?, ?)""",
                (
                    self.subject_id,
                    content_hash("request"),
                    content_hash({}),
                    self.clock_value,
                    self.clock_value,
                ),
            )
            connection.execute(
                """INSERT INTO relationship_social_runs(
                    social_id, subject_id, relationship_id, model_call_id, interaction_id,
                    idempotency_key, disposition, channel, counterparty, topic, rationale,
                    proposal_json, proposal_hash, evidence_event_ids_json, state_hash, created_at
                ) VALUES ('social_fixture', ?, ?, 'call_fixture', NULL,
                    'social-fixture', 'wait', 'none', 'nobody', 'wait', 'fixture', '{}',
                    ?, '[]', ?, ?)""",
                (
                    self.subject_id,
                    relationship.relationship_id,
                    content_hash({}),
                    content_hash({"fixture": True}),
                    self.clock_value,
                ),
            )
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute("DELETE FROM relationship_social_runs")

    def _interaction_event(self, interaction_id: str) -> str:
        with self.kernel.database.connection() as connection:
            row = connection.execute(
                "SELECT event_id FROM events WHERE event_type = 'interaction_received' "
                "AND json_extract(payload_json, '$.interaction_id') = ?",
                (interaction_id,),
            ).fetchone()
        assert row is not None
        return str(row["event_id"])


if __name__ == "__main__":
    unittest.main()
