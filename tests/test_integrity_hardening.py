from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from noyra.core import ActionLedger, Database, EventStore, IdentityStore
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash, new_id, utc_now


class IntegrityHardeningTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "noyra.sqlite3")
        self.subject_id = "Noyra-integrity-hardening"
        IdentityStore(self.database).ensure(self.subject_id, "a" * 64)
        self.events = EventStore(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_schema_marker_matches_installed_optional_features(self) -> None:
        with self.database.connection() as connection:
            version = int(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
            )
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }

        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertIn("interaction_transports", tables)
        self.assertIn("event_chain_roots", tables)
        with self.database.connection() as connection:
            model_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(model_calls)").fetchall()
            }
            segment_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(event_payload_segments)"
                ).fetchall()
            }
            observation_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(observations)").fetchall()
            }
        self.assertIn("capture_policy_version", model_columns)
        self.assertTrue(
            {"archive_format", "encryption_key_id", "encryption_key_fingerprint"} <= segment_columns
        )
        self.assertTrue({"content_archive_key", "content_archived_at"} <= observation_columns)

    def test_event_chain_uses_append_sequence_for_late_events(self) -> None:
        first = self.events.append(
            self.subject_id,
            "experience",
            "test",
            {"value": "first"},
            occurred_at="2026-08-14T00:00:00+00:00",
        )
        second = self.events.append(
            self.subject_id,
            "experience",
            "test",
            {"value": "late"},
            occurred_at="2026-08-13T00:00:00+00:00",
        )

        report = self.events.verify_chain(self.subject_id)
        self.assertEqual(report["event_count"], 2)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT event_id FROM event_chain_roots "
                "WHERE subject_id = ? ORDER BY sequence_number",
                (self.subject_id,),
            ).fetchall()
        self.assertEqual([row[0] for row in rows], [first.event_id, second.event_id])

    def test_causal_parent_cannot_be_newer_than_child(self) -> None:
        parent = self.events.append(
            self.subject_id,
            "experience",
            "test",
            {"value": "parent"},
            occurred_at="2099-08-14T00:00:00+00:00",
        )

        with self.assertRaises(ValueError):
            self.events.append(
                self.subject_id,
                "reflection",
                "test",
                {"value": "child"},
                causal_parent_ids=(parent.event_id,),
            )

    def test_action_revisions_detect_state_tampering(self) -> None:
        ledger = ActionLedger(self.database)
        action = ledger.prepare(
            self.subject_id,
            "observe",
            "test-tool",
            "target",
            {"value": 1},
        )
        ledger.start(action.action_id)
        ledger.finish(action.action_id, "succeeded", {"ok": True})

        report = ledger.verify_integrity(self.subject_id)
        self.assertEqual(report["action_revisions"], 3)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE actions SET result_json = ? WHERE action_id = ?",
                ('{"ok":false}', action.action_id),
            )
        with self.assertRaises(IntegrityError):
            ledger.verify_integrity(self.subject_id)

    def test_action_integrity_rejects_blob_json_storage(self) -> None:
        ledger = ActionLedger(self.database)
        action = ledger.prepare(
            self.subject_id,
            "observe",
            "test-tool",
            "blob-json-target",
            {"value": 1},
            resource_cost={"tokens": 2},
        )
        ledger.start(action.action_id)
        ledger.finish(action.action_id, "succeeded", {"ok": True})
        with self.database.connection() as connection:
            original = dict(
                connection.execute(
                    "SELECT resource_cost_json, result_json FROM actions WHERE action_id = ?",
                    (action.action_id,),
                ).fetchone()
            )

        for column in ("resource_cost_json", "result_json"):
            with self.subTest(column=column):
                value = original[column]
                assert isinstance(value, str)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE actions SET "{column}" = ? WHERE action_id = ?',
                        (sqlite3.Binary(value.encode("utf-8")), action.action_id),
                    )
                with self.assertRaises(IntegrityError):
                    ledger.verify_integrity(self.subject_id)
                with self.database.transaction() as connection:
                    connection.execute(
                        f'UPDATE actions SET "{column}" = ? WHERE action_id = ?',
                        (value, action.action_id),
                    )

    def test_database_rejects_cross_subject_action_goal(self) -> None:
        other_subject = "Noyra-integrity-other"
        IdentityStore(self.database).ensure(other_subject, "b" * 64)
        goal_id = new_id("goal")
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO goals(
                    goal_id, subject_id, title, description, origin, status,
                    priority, commitment, progress, emotional_pressure, state_hash,
                    current_revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'self', 'candidate', 0.5, 0.5, 0, 0, ?, 1, ?, ?)""",
                (
                    goal_id,
                    other_subject,
                    "other",
                    "other",
                    content_hash({"goal": goal_id}),
                    now,
                    now,
                ),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO actions(
                        action_id, subject_id, goal_id, project_id, phase_id, strategy_id,
                        action_type, tool, target, input_hash, idempotency_key,
                        expected_outcome, side_effect, status, retry_count,
                        resource_cost_json, result_json, prepared_at, started_at, completed_at
                    ) VALUES (?, ?, ?, NULL, NULL, NULL, 'observe', 'tool', 'target',
                              ?, ?, '', 0, 'prepared', 0, '{}', NULL, ?, NULL, NULL)""",
                    (
                        new_id("act"),
                        self.subject_id,
                        goal_id,
                        content_hash({"value": 1}),
                        new_id("idem"),
                        now,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
