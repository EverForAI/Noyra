from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from noyra.core import SubjectKernel
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.types import content_hash
from noyra.interaction import (
    InteractionDecision,
    InteractionIntegrity,
    InteractionStore,
    PublicDiaryStore,
    PublicProjection,
)
from noyra.interaction.errors import InteractionStateConflictError
from noyra.sleep import SleepEngine, SleepReflectionPlan


class InteractionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "noyra.sqlite3"
        self.subject_id = "Noyra-interaction-test"
        self.genesis_hash = content_hash({"seed": "interaction-test"})
        self.kernel = SubjectKernel(self.db_path, self.subject_id, self.genesis_hash)
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.interactions = InteractionStore(self.kernel.database)

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def test_human_message_is_an_invitation_and_subject_can_silence_it(self) -> None:
        invitation = self.interactions.receive(
            self.subject_id,
            "local",
            "human-founder",
            "Please tell me what you noticed today.",
            idempotency_key="incoming-1",
        )
        self.assertEqual(invitation.direction, "incoming")
        self.assertEqual(invitation.status, "offered")
        duplicate = self.interactions.receive(
            self.subject_id,
            "local",
            "human-founder",
            "Please tell me what you noticed today.",
            idempotency_key="incoming-1",
        )
        self.assertEqual(duplicate.interaction_id, invitation.interaction_id)
        decided = self.interactions.decide(
            invitation.interaction_id,
            InteractionDecision(
                disposition="silent", rationale="I do not want to communicate right now."
            ),
        )
        self.assertEqual(decided.status, "silent")
        with self.assertRaises(InteractionStateConflictError):
            self.interactions.decide(
                invitation.interaction_id,
                InteractionDecision(disposition="accepted", rationale="late response"),
            )
        self.assertEqual(
            InteractionIntegrity(self.kernel.database).verify(self.subject_id)[
                "interaction_decisions"
            ],
            1,
        )

    def test_subject_can_initiate_help_request_but_human_cannot_initiate_subject_message(
        self,
    ) -> None:
        request = self.interactions.send(
            self.subject_id,
            "local",
            "human-founder",
            "Could you verify whether this source is accessible?",
            kind="help_request",
            idempotency_key="help-1",
        )
        self.assertEqual(request.status, "sent")
        with self.assertRaises(PermissionError):
            self.interactions.send(
                self.subject_id,
                "local",
                "human-founder",
                "forged subject message",
                actor="human",
            )
        self.assertEqual(
            InteractionIntegrity(self.kernel.database).verify(self.subject_id)["interactions"],
            1,
        )

    def test_public_projection_excludes_private_state_and_diary_is_subject_selected(self) -> None:
        sleep = SleepEngine(self.kernel.database, self.subject_id)
        self.interactions.send(
            self.subject_id,
            "public:local",
            "human-founder",
            "I am choosing what to make public.",
            idempotency_key="publication-note",
        )
        self.interactions.send(
            self.subject_id,
            "local",
            "human-founder",
            "This outgoing message remains private.",
            idempotency_key="private-note",
        )
        run = sleep.start("subject_choice", "publish a selected public diary")
        sleep.begin_reflection(run.sleep_id)
        body = "Today I chose to keep one observation public."
        sleep.commit_reflection(
            run.sleep_id,
            SleepReflectionPlan(
                summary="I chose one bounded public account.",
                public_diary_candidate=body,
            ),
        )
        sleep.enter_deep_sleep(run.sleep_id)
        sleep.wake(run.sleep_id, "wake for publication", force=True)
        sleep.complete_wake(run.sleep_id)
        diaries = PublicDiaryStore(self.kernel.database)
        entry = diaries.publish(
            self.subject_id, run.sleep_id, "A bounded day", body, idempotency_key="diary-1"
        )
        self.assertEqual(entry.body, body)
        self.assertEqual(
            diaries.publish(
                self.subject_id,
                run.sleep_id,
                "A bounded day",
                body,
                idempotency_key="diary-1",
            ).entry_id,
            entry.entry_id,
        )
        with self.assertRaises(InteractionStateConflictError):
            diaries.publish(
                self.subject_id,
                run.sleep_id,
                "A different account",
                "A different private account.",
                idempotency_key="diary-2",
            )
        projection = PublicProjection(self.kernel.database)
        state = projection.state(self.subject_id)
        self.assertEqual(state["public_diary_count"], 1)
        self.assertNotIn("memories", state)
        self.assertEqual(projection.diary(self.subject_id)[0]["entry_id"], entry.entry_id)
        self.assertEqual(projection.interactions_view(self.subject_id)[0]["status"], "sent")
        self.assertEqual(len(projection.interactions_view(self.subject_id)), 1)
        with (
            self.assertRaises(sqlite3.IntegrityError),
            self.kernel.database.transaction() as connection,
        ):
            connection.execute(
                "DELETE FROM public_diary_entries WHERE entry_id = ?", (entry.entry_id,)
            )
        self.assertEqual(
            InteractionIntegrity(self.kernel.database).verify(self.subject_id)[
                "public_diary_entries"
            ],
            1,
        )

    def test_schema_migrates_from_version_five(self) -> None:
        legacy = Path(self.temp_dir.name) / "legacy-v5.sqlite3"
        raw = sqlite3.connect(legacy)
        raw.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        raw.execute("INSERT INTO schema_meta VALUES ('schema_version', '5')")
        raw.commit()
        raw.close()
        migrated = self.kernel.database.__class__(legacy)
        with migrated.connection() as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
