from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from noyra.core import EventStore, SubjectKernel
from noyra.core.types import content_hash
from noyra.mind import (
    AffectDecisionProfile,
    AffectImpulse,
    AffectPolicy,
    AppraisalInput,
    MindEngine,
)


class AffectPolicyTestCase(unittest.TestCase):
    def test_affect_changes_multiple_bounded_decision_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = SubjectKernel(
                Path(directory) / "noyra.sqlite3",
                "Noyra-affect-policy-test",
                content_hash({"seed": "affect-policy"}),
            )
            event = EventStore(kernel.database).append(
                kernel.subject_id, "experience", "test", {"topic": "unknown"}
            )
            MindEngine(kernel.database).process_event(
                kernel.subject_id,
                event.event_id,
                AppraisalInput(
                    novelty=0.9,
                    goal_congruence=0.4,
                    controllability=0.5,
                    certainty=0.3,
                    agency="subject",
                    narrative="An open question remains.",
                ),
                (
                    AffectImpulse(
                        emotion_type="curiosity",
                        target_type="world",
                        target_id=None,
                        impulse=0.9,
                        valence=0.6,
                        arousal=0.8,
                        dominance=0.4,
                        decay_rate=0.1,
                        goal_effect=0.5,
                    ),
                    AffectImpulse(
                        emotion_type="hope",
                        target_type="subject",
                        target_id=None,
                        impulse=0.7,
                        valence=0.8,
                        arousal=0.5,
                        dominance=0.5,
                        decay_rate=0.1,
                        goal_effect=0.4,
                    ),
                ),
                idempotency_key="affect-policy-fixture",
            )
            profile = AffectPolicy(kernel.database, kernel.subject_id).profile()
            self.assertIsInstance(profile, AffectDecisionProfile)
            self.assertGreater(profile.curiosity_drive, 0.5)
            self.assertGreater(profile.persistence, 0.5)
            self.assertGreater(profile.memory_salience, 0.5)
            self.assertGreater(profile.project_retry_limit(3), 3)
            self.assertLessEqual(profile.project_duration_multiplier(), 1.5)
