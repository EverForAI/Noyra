from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from noyra.core import Database, EventStore, IdentityStore, SubjectKernel
from noyra.core.errors import (
    DuplicateActionError,
    EventConflictError,
    IdentityConflictError,
    IntegrityError,
    InvalidTransitionError,
    NotFoundError,
    RuntimeOwnershipError,
)
from noyra.core.types import content_hash


class KernelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "noyra.sqlite3"
        self.subject_id = "Noyra-0001"
        self.genesis_hash = content_hash({"project": "Noyra", "seed": "test"})

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def new_kernel(self) -> SubjectKernel:
        return SubjectKernel(self.db_path, self.subject_id, self.genesis_hash)

    def test_schema_creation_and_foreign_keys(self) -> None:
        database = Database(self.db_path)
        with database.connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertTrue(
            {
                "subject_identity",
                "runtime_state",
                "events",
                "state_snapshots",
                "actions",
                "behavior_logs",
            }.issubset(tables)
        )
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(integrity, "ok")

        with self.assertRaises(sqlite3.IntegrityError):
            EventStore(database).append("missing-subject", "test", "test", {})

    def test_identity_survives_database_reopen(self) -> None:
        database = Database(self.db_path)
        first = IdentityStore(database).ensure(self.subject_id, self.genesis_hash)
        reopened = IdentityStore(Database(self.db_path)).load(self.subject_id)
        self.assertEqual(first.subject_id, reopened.subject_id)
        self.assertEqual(first.genesis_hash, reopened.genesis_hash)
        self.assertEqual(first.created_at, reopened.created_at)

    def test_identity_conflict_is_rejected(self) -> None:
        store = IdentityStore(Database(self.db_path))
        store.ensure(self.subject_id, self.genesis_hash)
        with self.assertRaises(IdentityConflictError):
            store.ensure(self.subject_id, "different-genesis")

    def test_identity_metadata_and_checkpoint_updates_are_validated(self) -> None:
        database = Database(self.db_path)
        store = IdentityStore(database)
        with self.assertRaises(ValueError):
            store.ensure("x", self.genesis_hash)
        store.ensure(self.subject_id, self.genesis_hash)
        renamed = store.set_personal_name(self.subject_id, "Noyra")
        self.assertEqual(renamed.personal_name, "Noyra")

        kernel = self.new_kernel()
        snapshot = kernel.snapshot_store.save(
            self.subject_id,
            {"state": "manual"},
            state_version=1,
            reason="manual checkpoint",
        )
        updated = store.update_checkpoint(
            self.subject_id,
            state_version=1,
            checkpoint_id=snapshot.snapshot_id,
            model_name="test-model",
        )
        self.assertEqual(updated.last_checkpoint, snapshot.snapshot_id)
        self.assertEqual(updated.model_name, "test-model")
        with self.assertRaises(IdentityConflictError):
            store.update_checkpoint(
                self.subject_id,
                state_version=1,
                checkpoint_id=snapshot.snapshot_id,
            )

    def test_event_payload_hash_is_verified_on_read(self) -> None:
        database = Database(self.db_path)
        IdentityStore(database).ensure(self.subject_id, self.genesis_hash)
        events = EventStore(database)
        event = events.append(self.subject_id, "test_event", "test", {"b": 2, "a": 1})
        loaded = events.get(event.event_id)
        self.assertEqual(loaded.payload, {"b": 2, "a": 1})
        self.assertEqual(loaded.payload_hash, content_hash({"b": 2, "a": 1}))

        with (
            database.transaction() as connection,
            self.assertRaisesRegex(Exception, "event evidence is append-only"),
        ):
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE event_id = ?",
                ('{"tampered":true}', event.event_id),
            )
        self.assertEqual(events.verify_chain(self.subject_id)["event_count"], 1)

    def test_event_id_is_idempotent_but_conflicts_are_rejected(self) -> None:
        kernel = self.new_kernel()
        first = kernel.event_store.append(
            self.subject_id, "observation", "test", {"value": 1}, event_id="evt_fixed"
        )
        duplicate = kernel.event_store.append(
            self.subject_id, "observation", "test", {"value": 1}, event_id="evt_fixed"
        )
        self.assertEqual(first.event_id, duplicate.event_id)
        with self.assertRaises(EventConflictError):
            kernel.event_store.append(
                self.subject_id,
                "observation",
                "test",
                {"value": 2},
                event_id="evt_fixed",
            )

    def test_event_processing_lifecycle_is_queryable_and_validated(self) -> None:
        kernel = self.new_kernel()
        first = kernel.event_store.append(self.subject_id, "first", "test", {"order": 1})
        kernel.event_store.append(self.subject_id, "second", "test", {"order": 2})
        kernel.event_store.mark_processed(first.event_id, subject_id=self.subject_id)
        processed = kernel.event_store.list(self.subject_id, status="processed")
        self.assertEqual([event.event_id for event in processed], [first.event_id])
        with self.assertRaises(ValueError):
            kernel.event_store.mark_processed(
                first.event_id, "arbitrary", subject_id=self.subject_id
            )
        with self.assertRaises(NotFoundError):
            kernel.event_store.mark_processed("evt_missing", subject_id=self.subject_id)

    def test_lifecycle_records_valid_transitions_and_rejects_invalid_ones(self) -> None:
        kernel = self.new_kernel()
        with self.assertRaises(RuntimeOwnershipError):
            kernel.activate()
        kernel.boot()
        with self.assertRaises(InvalidTransitionError):
            kernel.pause()
        kernel.orient()
        active = kernel.activate()
        self.assertEqual(active.state, "active")
        lifecycle_events = [
            event
            for event in kernel.event_store.list(self.subject_id)
            if event.event_type == "lifecycle_transition"
        ]
        self.assertEqual(len(lifecycle_events), 3)

    def test_restart_from_active_preserves_identity_and_records_recovery(self) -> None:
        first = self.new_kernel()
        first.boot()
        first.activate()

        reopened = self.new_kernel()
        with self.assertRaises(RuntimeOwnershipError):
            reopened.boot()
        first.close()
        recovered = reopened.boot()
        self.assertEqual(recovered.state, "booting")
        assert reopened.identity is not None
        assert first.identity is not None
        self.assertEqual(reopened.identity.subject_id, first.identity.subject_id)
        events = reopened.event_store.list(self.subject_id)
        self.assertEqual(events[0].event_type, "lifecycle_recovery")
        self.assertEqual(events[0].payload["from"], "active")

    def test_lifecycle_pause_sleep_wake_and_stop_path(self) -> None:
        kernel = self.new_kernel()
        kernel.boot()
        kernel.activate()
        self.assertEqual(kernel.pause("reflection requested").state, "paused")
        self.assertEqual(kernel.resume().state, "active")
        self.assertEqual(
            kernel.lifecycle.transition("winding_down", "fatigue threshold").state,
            "winding_down",
        )
        kernel.lifecycle.transition("reflective_sleep", "begin consolidation")
        kernel.lifecycle.transition("deep_sleep", "consolidation complete")
        kernel.lifecycle.transition("waking", "scheduled wake")
        kernel.activate()
        self.assertEqual(kernel.stop().state, "stopped")
        self.assertEqual(kernel.boot().state, "booting")

    def test_checkpoint_updates_identity_and_is_atomic(self) -> None:
        kernel = self.new_kernel()
        kernel.boot()
        with (
            mock.patch.object(
                kernel.identity_store,
                "_update_checkpoint_connection",
                side_effect=RuntimeError("injected failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected failure"),
        ):
            kernel.checkpoint({"state": "not committed"}, reason="failure injection")

        self.assertEqual(kernel.identity_store.load(self.subject_id).state_version, 0)
        with self.assertRaises(NotFoundError):
            kernel.snapshot_store.latest(self.subject_id)

        snapshot = kernel.checkpoint({"state": "initial"}, reason="test checkpoint")
        reopened = self.new_kernel()
        self.assertEqual(
            reopened.identity_store.load(self.subject_id).last_checkpoint,
            snapshot.snapshot_id,
        )
        self.assertEqual(
            reopened.snapshot_store.latest(self.subject_id).state, {"state": "initial"}
        )

    def test_concurrent_checkpoints_receive_unique_monotonic_versions(self) -> None:
        first = self.new_kernel()
        first.boot()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(first.checkpoint, {"writer": 1}, reason="writer one"),
                executor.submit(first.checkpoint, {"writer": 2}, reason="writer two"),
            ]
            snapshots = [future.result(timeout=10) for future in futures]
        self.assertEqual({snapshot.state_version for snapshot in snapshots}, {1, 2})
        reopened = self.new_kernel()
        assert reopened.identity is not None
        self.assertEqual(reopened.identity.state_version, 2)
        self.assertEqual(reopened.snapshot_store.latest(self.subject_id).state_version, 2)

    def test_snapshot_hash_and_identity_continuity_are_verified(self) -> None:
        kernel = self.new_kernel()
        kernel.boot()
        snapshot = kernel.checkpoint({"stable": True}, reason="integrity test")
        with kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE state_snapshots SET state_json = ? WHERE snapshot_id = ?",
                ('{"stable":false}', snapshot.snapshot_id),
            )
        with self.assertRaises(IntegrityError):
            self.new_kernel()

    def test_orphan_snapshot_is_rejected_at_version_zero(self) -> None:
        kernel = self.new_kernel()
        kernel.snapshot_store.save(
            self.subject_id,
            {"orphan": True},
            state_version=1,
            reason="fault simulation",
        )
        with self.assertRaisesRegex(IntegrityError, "unexpectedly has snapshots"):
            self.new_kernel()

    def test_action_idempotency_is_subject_scoped_and_collision_safe(self) -> None:
        kernel = self.new_kernel()
        other_subject = "Noyra-0002"
        kernel.identity_store.ensure(other_subject, content_hash({"seed": "other"}))
        first = kernel.action_ledger.prepare(
            self.subject_id,
            "observe",
            "test-tool",
            "private-target",
            {"x": 1},
            idempotency_key="shared-key",
        )
        duplicate = kernel.action_ledger.prepare(
            self.subject_id,
            "observe",
            "test-tool",
            "private-target",
            {"x": 1},
            idempotency_key="shared-key",
        )
        other = kernel.action_ledger.prepare(
            other_subject,
            "observe",
            "test-tool",
            "private-target",
            {"x": 1},
            idempotency_key="shared-key",
        )
        self.assertEqual(first.action_id, duplicate.action_id)
        self.assertNotEqual(first.action_id, other.action_id)
        with self.assertRaises(DuplicateActionError):
            kernel.action_ledger.prepare(
                self.subject_id,
                "observe",
                "test-tool",
                "another-target",
                {"x": 1},
                idempotency_key="shared-key",
            )

    def test_interrupted_action_is_quarantined_and_not_executed_twice(self) -> None:
        kernel = self.new_kernel()
        action = kernel.action_ledger.prepare(
            self.subject_id,
            "publish",
            "test-tool",
            "secret-target",
            {"message": "hello"},
            side_effect=True,
        )
        kernel.action_ledger.start(action.action_id)

        reopened = self.new_kernel()
        self.assertEqual(reopened.recovered_actions, [])
        reopened.boot()
        self.assertEqual(
            [item.action_id for item in reopened.recovered_actions], [action.action_id]
        )
        recovered = reopened.action_ledger.recoverable(self.subject_id)
        self.assertEqual(recovered[0].status, "unknown")
        with self.assertRaises(InvalidTransitionError):
            reopened.action_ledger.start(action.action_id)

        reopened.close()
        opened_again = self.new_kernel()
        opened_again.boot()
        self.assertEqual(opened_again.recovered_actions, [])
        logs = opened_again.action_ledger.behavior_logs(self.subject_id)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["public_target"], "[private]")
        self.assertEqual(logs[0]["result_status"], "unknown")

        reconciled = opened_again.action_ledger.reconcile_unknown(
            action.action_id,
            "succeeded",
            {"confirmed": True},
            public_explanation="Outcome confirmed.",
        )
        self.assertEqual(reconciled.status, "succeeded")
        self.assertEqual(
            opened_again.action_ledger.behavior_logs(self.subject_id)[0]["result_status"],
            "succeeded",
        )

    def test_completed_action_creates_privacy_safe_behavior_log(self) -> None:
        kernel = self.new_kernel()
        action = kernel.action_ledger.prepare(
            self.subject_id, "observe", "test-tool", "sensitive-path", {"x": 1}
        )
        kernel.action_ledger.start(action.action_id)
        finished = kernel.action_ledger.finish(
            action.action_id,
            "succeeded",
            {"ok": True},
            public_explanation="Observation completed.",
        )
        self.assertEqual(finished.status, "succeeded")
        logs = kernel.action_ledger.behavior_logs(self.subject_id)
        self.assertEqual(logs[0]["public_target"], "[private]")
        self.assertEqual(logs[0]["redaction_reason"], "target withheld by default")
        with self.assertRaises(InvalidTransitionError):
            kernel.action_ledger.start(action.action_id)

    def test_prepared_action_can_be_cancelled_without_exposing_target(self) -> None:
        kernel = self.new_kernel()
        action = kernel.action_ledger.prepare(
            self.subject_id, "observe", "test-tool", "sensitive-path", {"x": 1}
        )
        cancelled = kernel.action_ledger.cancel(action.action_id, "superseded")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.result, {"reason": "superseded"})
        self.assertEqual(
            kernel.action_ledger.behavior_logs(self.subject_id)[0]["public_target"],
            "[private]",
        )
        with self.assertRaises(InvalidTransitionError):
            kernel.action_ledger.cancel(action.action_id, "again")
        with self.assertRaises(NotFoundError):
            kernel.action_ledger.start("act_missing")


if __name__ == "__main__":
    unittest.main()
