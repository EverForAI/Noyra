from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from noyra.cognition import (
    CognitionSettings,
    EpistemicReview,
    MetacognitiveControl,
    WorldSourceConfig,
)
from noyra.core import SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.mind import BeliefStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelUsage,
    ProviderResponse,
)
from noyra.world import (
    FetchedDocument,
    ObservationStore,
    PredictionProposal,
    PredictionStore,
    SourceRegistry,
)
from noyra.world.errors import WorldStateConflictError
from noyra.world.types import ObservationRecord


class EpistemicReviewTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.subject_id = "Noyra-epistemic-test"
        self.kernel = SubjectKernel(
            Path(self.temp_dir.name) / "noyra.sqlite3",
            self.subject_id,
            content_hash({"seed": "epistemic-test"}),
        )
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.clock_value = "2026-08-12T12:00:00.000+00:00"
        self.source = SourceRegistry(self.kernel.database).register(
            self.subject_id,
            "Evidence source",
            "https://example.com/evidence",
            "news",
            trust_score=0.8,
            status="active",
        )

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def settings(self) -> CognitionSettings:
        return CognitionSettings(
            enabled=True,
            sources=(
                WorldSourceConfig(
                    name="Evidence source",
                    url="https://example.com/evidence",
                    source_type="news",
                    trust_score=0.8,
                ),
            ),
            max_epistemic_review_model_calls_per_day=4,
            max_belief_confidence_delta=0.15,
        )

    def observation(self, label: str, *, analyzed: bool = True) -> ObservationRecord:
        content = f"Analyzed evidence {label}."
        record = ObservationStore(self.kernel.database).record(
            self.subject_id,
            self.source.source_id,
            FetchedDocument(
                url=self.source.url,
                title=label,
                content=content,
                content_hash=content_hash(content),
                media_type="text/plain",
                injection_signals=(),
                etag=None,
                last_modified=None,
                fetched_at=self.clock_value,
            ),
        )[0]
        if analyzed:
            record = ObservationStore(self.kernel.database).mark(
                record.observation_id, "analyzed", subject_id=self.subject_id
            )
        return record

    def gateway(self, proposal: Mapping[str, object]) -> ModelGateway:
        provider = FakeProvider(
            [
                ProviderResponse(
                    content=json.dumps(proposal),
                    usage=ModelUsage(100, 50),
                    finish_reason="stop",
                )
            ]
        )
        return ModelGateway(
            provider,
            ModelLedger(self.kernel.database),
            model="epistemic-model",
            limits=BudgetLimits(20, 100_000, 100_000, 1_000_000),
        )

    async def no_change_review(self) -> EpistemicReview:
        self.observation("integrity")
        proposal = {
            "summary": "The analyzed evidence does not justify a state change.",
            "belief_revisions": [],
            "prediction_resolutions": [],
        }
        review = EpistemicReview(
            self.kernel.database,
            self.subject_id,
            self.gateway(proposal),
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await review.run_due(), "epistemic_review_no_change")
        return review

    async def test_revises_belief_and_resolves_due_prediction_from_analyzed_evidence(self) -> None:
        original = self.observation("original")
        counter = self.observation("counter")
        belief = BeliefStore(self.kernel.database).create(
            self.subject_id,
            "The measured pattern will remain stable.",
            confidence=0.7,
            scope="public evidence",
            supporting_event_ids=(original.event_id,),
        )
        prediction = PredictionStore(
            self.kernel.database, clock=lambda: "2026-08-10T00:00:00.000+00:00"
        ).create(
            self.subject_id,
            PredictionProposal(
                statement="The pattern will remain stable by August 12.",
                probability=0.7,
                target_at="2026-08-12T00:00:00+00:00",
                resolution_criteria="Analyzed evidence reports instability.",
            ),
            evidence_observation_ids=(original.observation_id,),
        )
        proposal = {
            "summary": "Counterevidence lowers confidence and settles the due forecast.",
            "belief_revisions": [
                {
                    "belief_id": belief.belief_id,
                    "disposition": "weaken",
                    "proposition": belief.proposition,
                    "confidence": 0.58,
                    "supporting_observation_ids": [],
                    "counter_observation_ids": [counter.observation_id],
                    "reason": "The analyzed counterevidence reports instability.",
                }
            ],
            "prediction_resolutions": [
                {
                    "prediction_id": prediction.prediction_id,
                    "disposition": "resolve_false",
                    "evidence_observation_ids": [counter.observation_id],
                    "rationale": "The resolution criterion is directly contradicted.",
                }
            ],
        }
        review = EpistemicReview(
            self.kernel.database,
            self.subject_id,
            self.gateway(proposal),
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await review.run_due(), "epistemic_review_committed")
        self.assertAlmostEqual(
            BeliefStore(self.kernel.database).get(belief.belief_id).confidence, 0.58
        )
        resolved = PredictionStore(self.kernel.database).get(prediction.prediction_id)
        self.assertEqual(resolved.outcome, False)
        self.assertAlmostEqual(resolved.brier_score or 0, 0.49)
        latest = review.latest()
        assert latest is not None
        self.assertEqual(latest.status, "committed")
        self.assertEqual(latest.applied_belief_ids, (belief.belief_id,))

    async def test_rejects_unanalyzed_evidence_and_excessive_confidence_change(self) -> None:
        analyzed = self.observation("analyzed")
        pending = self.observation("pending", analyzed=False)
        belief = BeliefStore(self.kernel.database).create(
            self.subject_id,
            "A bounded belief.",
            confidence=0.5,
            scope="test",
            supporting_event_ids=(analyzed.event_id,),
        )
        proposal = {
            "summary": "Invalid model proposal.",
            "belief_revisions": [
                {
                    "belief_id": belief.belief_id,
                    "disposition": "strengthen",
                    "proposition": belief.proposition,
                    "confidence": 0.9,
                    "supporting_observation_ids": [pending.observation_id],
                    "counter_observation_ids": [],
                    "reason": "Invalid evidence.",
                }
            ],
            "prediction_resolutions": [],
        }
        review = EpistemicReview(
            self.kernel.database,
            self.subject_id,
            self.gateway(proposal),
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(await review.run_due(), "epistemic_review_rejected")
        self.assertEqual(
            BeliefStore(self.kernel.database).get(belief.belief_id).current_revision, 1
        )

    def test_prediction_resolution_requires_analyzed_observation(self) -> None:
        pending = self.observation("pending", analyzed=False)
        store = PredictionStore(self.kernel.database, clock=lambda: "2026-08-10T00:00:00.000+00:00")
        prediction = store.create(
            self.subject_id,
            PredictionProposal(
                statement="A due test prediction.",
                probability=0.5,
                target_at="2026-08-11T00:00:00+00:00",
                resolution_criteria="Analyzed evidence settles it.",
            ),
            evidence_observation_ids=(pending.observation_id,),
        )
        due_store = PredictionStore(self.kernel.database, clock=lambda: self.clock_value)
        with self.assertRaises(WorldStateConflictError):
            due_store.resolve(
                prediction.prediction_id,
                outcome=False,
                evidence_observation_ids=(pending.observation_id,),
                rationale="Unanalyzed evidence is insufficient.",
            )

    async def test_metacognitive_integrity_covers_epistemic_review_json(self) -> None:
        review = await self.no_change_review()
        self.assertEqual(review.verify_integrity(), 1)
        control = MetacognitiveControl(
            self.kernel.database,
            self.subject_id,
            self.settings(),
            clock=lambda: self.clock_value,
        )
        self.assertEqual(control.verify_integrity()["epistemic_review_runs"], 1)
        with self.kernel.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_epistemic_review_update")
            row = connection.execute(
                "SELECT review_id, proposal_json FROM epistemic_review_runs"
            ).fetchone()
            connection.execute(
                "UPDATE epistemic_review_runs SET proposal_json = ? WHERE review_id = ?",
                (row["proposal_json"].encode("utf-8"), row["review_id"]),
            )
        with self.assertRaises(IntegrityError):
            control.verify_integrity()

    async def test_epistemic_integrity_rejects_matching_hash_invalid_status(self) -> None:
        review = await self.no_change_review()
        latest = review.latest()
        assert latest is not None
        invalid_state: dict[str, object] = {
            "model_call_id": latest.model_call_id,
            "status": "committed",
            "trigger_observation_id": latest.trigger_observation_id,
            "belief_ids": [],
            "prediction_ids": [],
            "summary": latest.summary,
        }
        with self.kernel.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_epistemic_review_update")
            connection.execute(
                "UPDATE epistemic_review_runs SET status = 'committed', state_hash = ? "
                "WHERE review_id = ?",
                (content_hash(invalid_state), latest.review_id),
            )
        with self.assertRaises(IntegrityError):
            review.verify_integrity()


if __name__ == "__main__":
    unittest.main()
