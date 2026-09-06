from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from noyra.core import ActionLedger, Database, IdentityStore
from noyra.core.database import CURRENT_SCHEMA_VERSION, behavior_log_state_hash
from noyra.core.errors import IntegrityError
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.types import content_hash, utc_now


class BehaviorReconciliationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "noyra.sqlite3")
        self.subject_id = "Noyra-reconcile-test"
        IdentityStore(self.database).ensure(
            self.subject_id, content_hash({"subject_id": self.subject_id})
        )
        self.ledger = ActionLedger(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _unknown_action(self) -> str:
        action = self.ledger.prepare(
            self.subject_id,
            "publish",
            "test-tool",
            "private-target",
            {"value": 1},
            side_effect=True,
        )
        self.ledger.start(action.action_id)
        self.ledger.finish(
            action.action_id,
            "unknown",
            {"outcome_unknown": True},
            side_effect_summary="completion may have happened",
            public_explanation="Completion was not durably observed.",
        )
        return action.action_id

    @staticmethod
    def _insert_revision(connection: sqlite3.Connection, revision: dict[str, object]) -> None:
        connection.execute(
            """INSERT INTO behavior_log_revisions(
                revision_id, log_id, action_id, subject_id, revision_number,
                occurred_at, action_type, public_goal_reference, tool, public_target,
                result_status, side_effect_summary, resource_summary, public_explanation,
                redaction_reason, state_hash, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(
                revision[field]
                for field in (
                    "revision_id",
                    "log_id",
                    "action_id",
                    "subject_id",
                    "revision_number",
                    "occurred_at",
                    "action_type",
                    "public_goal_reference",
                    "tool",
                    "public_target",
                    "result_status",
                    "side_effect_summary",
                    "resource_summary",
                    "public_explanation",
                    "redaction_reason",
                    "state_hash",
                    "reason",
                    "created_at",
                )
            ),
        )

    def test_reconciliation_preserves_original_and_appends_hashed_revision(self) -> None:
        action_id = self._unknown_action()
        with self.database.connection() as connection:
            original_log = connection.execute(
                "SELECT * FROM behavior_logs WHERE action_id = ?", (action_id,)
            ).fetchone()
            original_revision = connection.execute(
                "SELECT * FROM behavior_log_revisions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        assert original_log is not None
        assert original_revision is not None

        self.ledger.reconcile_unknown(
            action_id,
            "succeeded",
            {"confirmed": True},
            public_explanation="Outcome confirmed by reconciliation.",
        )

        with self.database.connection() as connection:
            restored_log = connection.execute(
                "SELECT * FROM behavior_logs WHERE action_id = ?", (action_id,)
            ).fetchone()
            revisions = connection.execute(
                "SELECT revision_number, result_status, state_hash, reason "
                "FROM behavior_log_revisions WHERE action_id = ? ORDER BY revision_number",
                (action_id,),
            ).fetchall()
        assert restored_log is not None
        self.assertEqual(tuple(restored_log), tuple(original_log))
        self.assertEqual([row[0] for row in revisions], [1, 2])
        self.assertEqual([row[1] for row in revisions], ["unknown", "succeeded"])
        self.assertTrue(all(len(str(row[2])) == 64 for row in revisions))
        self.assertNotEqual(revisions[0][2], revisions[1][2])
        self.assertEqual(revisions[1][3], "unknown outcome reconciled")

        latest = self.ledger.behavior_logs(self.subject_id)[0]
        self.assertEqual(latest["result_status"], "succeeded")
        self.assertEqual(latest["original_result_status"], "unknown")
        self.assertEqual(latest["behavior_revision_number"], 2)
        self.assertEqual(
            latest["original_public_explanation"],
            "Completion was not durably observed.",
        )
        self.assertEqual(self.ledger.verify_integrity(self.subject_id)["behavior_log_revisions"], 2)

        with (
            self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"),
            self.database.transaction() as connection,
        ):
            connection.execute(
                "UPDATE behavior_logs SET public_explanation = 'tampered' WHERE action_id = ?",
                (action_id,),
            )
        with (
            self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be deleted"),
            self.database.transaction() as connection,
        ):
            connection.execute("DELETE FROM behavior_logs WHERE action_id = ?", (action_id,))
        with (
            self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"),
            self.database.transaction() as connection,
        ):
            connection.execute(
                "UPDATE behavior_log_revisions SET reason = 'tampered' WHERE action_id = ?",
                (action_id,),
            )

    def test_revision_contract_rejects_extra_or_mutated_rows_and_hash_tampering(self) -> None:
        action_id = self._unknown_action()
        with self.database.connection() as connection:
            first = connection.execute(
                "SELECT * FROM behavior_log_revisions WHERE action_id = ?", (action_id,)
            ).fetchone()
        assert first is not None
        mutated = dict(first)
        mutated.update(
            {
                "revision_id": "brev-mutated",
                "revision_number": 2,
                "public_target": "foreign-target",
                "result_status": "unknown",
                "reason": "tampered",
                "created_at": utc_now(),
            }
        )
        mutated["state_hash"] = behavior_log_state_hash(mutated)
        with (
            self.assertRaisesRegex(sqlite3.IntegrityError, "reconciliation contract"),
            self.database.transaction() as connection,
        ):
            self._insert_revision(connection, mutated)

        self.ledger.reconcile_unknown(action_id, "succeeded", {"confirmed": True})
        with self.database.connection() as connection:
            latest = dict(
                connection.execute(
                    "SELECT * FROM behavior_log_revisions WHERE action_id = ? "
                    "ORDER BY revision_number DESC LIMIT 1",
                    (action_id,),
                ).fetchone()
            )
        latest.update(
            {
                "revision_id": "brev-extra",
                "revision_number": 3,
                "public_target": "foreign-target",
                "reason": "extra revision",
                "created_at": utc_now(),
            }
        )
        latest["state_hash"] = behavior_log_state_hash(latest)
        with (
            self.assertRaisesRegex(sqlite3.IntegrityError, "reconciliation contract"),
            self.database.transaction() as connection,
        ):
            self._insert_revision(connection, latest)

        with self.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_behavior_log_revision_update")
            connection.execute(
                "UPDATE behavior_log_revisions SET state_hash = ? WHERE action_id = ?",
                ("0" * 64, action_id),
            )
        with self.assertRaisesRegex(IntegrityError, "integrity mismatch"):
            self.ledger.verify_integrity(self.subject_id)

    def test_schema_32_behavior_log_migration_creates_baseline_and_is_idempotent(self) -> None:
        action_id = self._unknown_action()
        database_path = self.root / "noyra.sqlite3"
        with self.database.connection() as connection:
            original = tuple(
                connection.execute(
                    "SELECT * FROM behavior_logs WHERE action_id = ?", (action_id,)
                ).fetchone()
            )
        with self.database.transaction() as connection:
            for trigger in (
                "validate_behavior_log_revision_insert",
                "prevent_behavior_log_revision_update",
                "prevent_behavior_log_revision_delete",
                "prevent_behavior_log_update",
                "prevent_behavior_log_delete",
            ):
                connection.execute(f"DROP TRIGGER {trigger}")
            connection.execute("DROP TABLE behavior_log_revisions")
            connection.execute("UPDATE schema_meta SET value = '32' WHERE key = 'schema_version'")

        migrated = Database(database_path)
        with migrated.connection() as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            restored = tuple(
                connection.execute(
                    "SELECT * FROM behavior_logs WHERE action_id = ?", (action_id,)
                ).fetchone()
            )
            baseline = dict(
                connection.execute(
                    "SELECT * FROM behavior_log_revisions WHERE action_id = ?", (action_id,)
                ).fetchone()
            )
        self.assertEqual(int(version), CURRENT_SCHEMA_VERSION)
        self.assertEqual(restored, original)
        self.assertEqual(baseline["revision_number"], 1)
        self.assertEqual(baseline["result_status"], "unknown")
        self.assertEqual(baseline["state_hash"], behavior_log_state_hash(baseline))

        restarted = Database(database_path)
        with restarted.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM behavior_log_revisions WHERE action_id = ?",
                    (action_id,),
                ).fetchone()[0],
                1,
            )

    def test_current_schema_missing_revision_fails_closed_without_repair(self) -> None:
        action_id = self._unknown_action()
        database_path = self.root / "noyra.sqlite3"
        with self.database.connection() as connection:
            original = tuple(
                connection.execute(
                    "SELECT * FROM behavior_logs WHERE action_id = ?", (action_id,)
                ).fetchone()
            )
        with self.database.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_behavior_log_revision_delete")
            connection.execute(
                "DELETE FROM behavior_log_revisions WHERE action_id = ?", (action_id,)
            )

        with self.assertRaisesRegex(RuntimeError, "revision history is incomplete"):
            Database(database_path)

        with self.database.connection() as connection:
            marker = int(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
            )
            restored = tuple(
                connection.execute(
                    "SELECT * FROM behavior_logs WHERE action_id = ?", (action_id,)
                ).fetchone()
            )
            revision_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM behavior_log_revisions WHERE action_id = ?",
                    (action_id,),
                ).fetchone()[0]
            )
        self.assertEqual(marker, CURRENT_SCHEMA_VERSION)
        self.assertEqual(restored, original)
        self.assertEqual(revision_count, 0)

    def test_runtime_export_contains_original_and_reconciled_chain(self) -> None:
        action_id = self._unknown_action()
        self.ledger.reconcile_unknown(action_id, "failed", {"confirmed": False})
        artifact = RuntimeLogExporter(self.database).export(self.subject_id, actor="test")

        with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
            revisions = [
                json.loads(line)
                for line in archive.read("tables/behavior_log_revisions.jsonl").splitlines()
            ]
            logs = [
                json.loads(line) for line in archive.read("tables/behavior_logs.jsonl").splitlines()
            ]
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["result_status"], "unknown")
        self.assertEqual([row["revision_number"] for row in revisions], [1, 2])
        self.assertEqual([row["result_status"] for row in revisions], ["unknown", "failed"])
        self.assertTrue(all(len(row["state_hash"]) == 64 for row in revisions))
        self.assertIn(
            "tables/behavior_log_revisions.jsonl", {item["file"] for item in manifest["tables"]}
        )


if __name__ == "__main__":
    unittest.main()
