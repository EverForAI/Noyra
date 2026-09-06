from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from noyra.cognition import CognitionSettings, SemanticMemoryIntegrator
from noyra.core import EventStore, SubjectKernel
from noyra.core.types import content_hash
from noyra.mind import MemoryStore
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelPricing,
    ModelUsage,
    ProviderResponse,
    RetryPolicy,
)


class SemanticMemoryIntegrationTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_model_candidate_is_supervised_and_originals_remain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = SubjectKernel(
                Path(directory) / "noyra.sqlite3",
                "Noyra-semantic-integration",
                content_hash({"seed": "semantic-integration"}),
            )
            event = EventStore(kernel.database).append(
                kernel.subject_id, "observation", "test", {"topic": "continuity"}
            )
            memories = MemoryStore(kernel.database)
            first = memories.create(
                kernel.subject_id,
                "semantic",
                "Continuity needs state.",
                salience=0.8,
                confidence=0.8,
                source_event_ids=(event.event_id,),
            )
            second = memories.create(
                kernel.subject_id,
                "semantic",
                "Continuity needs history.",
                salience=0.8,
                confidence=0.8,
                source_event_ids=(event.event_id,),
            )
            proposal = json.dumps(
                {
                    "disposition": "integrate",
                    "operation": "merge",
                    "source_memory_ids": [first.memory_id, second.memory_id],
                    "synthesis": "Continuity needs both state and history.",
                    "confidence": 0.8,
                    "evidence_event_ids": [event.event_id],
                    "reason": "Both memories describe complementary requirements.",
                }
            )
            provider = FakeProvider(
                [ProviderResponse(content=proposal, usage=ModelUsage(500, 200))]
            )
            gateway = ModelGateway(
                provider,
                ModelLedger(kernel.database),
                model="test-model",
                limits=BudgetLimits(10, 100_000, 100_000, 1_000_000),
                pricing=ModelPricing(),
                retry_policy=RetryPolicy(max_attempts=1),
            )
            integrator = SemanticMemoryIntegrator(
                kernel.database,
                kernel.subject_id,
                gateway,
                CognitionSettings(),
            )
            self.assertEqual(await integrator.run_due(), "memory_integration_committed")
            self.assertEqual(memories.get(first.memory_id).status, "superseded")
            with kernel.database.connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 3
                )
