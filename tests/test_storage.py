from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from noyra.core import (
    ArchiveStore,
    Database,
    EventStore,
    IdentityStore,
    StorageLayout,
    TrainingStore,
)
from noyra.core.training_export import TrainingDatasetExporter
from noyra.core.types import content_hash
from noyra.model import ModelLedger


class StorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = Database(self.root / "subject.sqlite3")
        self.subject_id = "Noyra-storage-test"
        identities = IdentityStore(self.database)
        identities.ensure(self.subject_id, content_hash({"subject": self.subject_id}))
        self.subject_storage_key = identities.storage_key(self.subject_id)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_layout_isolated_and_traversal_safe(self) -> None:
        layout = StorageLayout.create(self.root / "runtime")
        self.assertTrue(layout.subject.is_dir())
        self.assertTrue(layout.workspace.is_dir())
        self.assertEqual(
            layout.path_for("workspace", "project/main.py"),
            layout.workspace / "project" / "main.py",
        )
        with self.assertRaises(ValueError):
            layout.path_for("workspace", "../subject/secret")

    def test_events_create_immutable_training_provenance(self) -> None:
        event = EventStore(self.database).append(
            self.subject_id, "observation", "test", {"value": 1}, privacy_level="public"
        )
        records = TrainingStore(self.database).list_records(self.subject_id)
        self.assertEqual(records[0].event_id, event.event_id)
        self.assertEqual(records[0].source_hash, event.payload_hash)
        with (
            self.database.transaction() as connection,
            self.assertRaisesRegex(Exception, "training records cannot be deleted"),
        ):
            connection.execute("DELETE FROM training_records WHERE event_id = ?", (event.event_id,))

    def test_private_psychology_is_excluded_by_default(self) -> None:
        event = EventStore(self.database).append(
            self.subject_id, "sleep_reflection", "sleep", {"thought": "private"}
        )
        record = TrainingStore(self.database).list_records(self.subject_id)[0]
        self.assertEqual(record.event_id, event.event_id)
        self.assertEqual(record.eligibility, "excluded")
        self.assertEqual(record.redaction_status, "excluded")

    def test_archive_registration_hashes_bytes(self) -> None:
        archive = ArchiveStore(self.database).register(
            self.subject_id,
            storage_class="cold",
            object_key="2026/events.ndjson.zst.enc",
            byte_size=3,
            payload=b"abc",
            status="verified",
        )
        self.assertEqual(archive.byte_size, 3)
        self.assertEqual(len(archive.content_hash), 64)
        self.assertIsNotNone(archive.verified_at)

    def test_private_psychology_can_be_explicitly_included_and_redacted(self) -> None:
        store = TrainingStore(self.database)
        store.update_policy(self.subject_id, include_private_psychology=True)
        event = EventStore(self.database).append(
            self.subject_id,
            "sleep_reflection",
            "sleep",
            {"thought": "reviewed", "api_key": "must-redact"},
        )
        record = store.list_records(self.subject_id)[0]
        self.assertEqual(record.event_id, event.event_id)
        self.assertEqual(record.eligibility, "eligible")
        self.assertEqual(record.redaction_status, "redacted")
        with self.database.connection() as connection:
            audit = connection.execute(
                "SELECT action, actor, payload_json FROM audit_records "
                "WHERE subject_id = ? AND action = 'training_policy_updated' "
                "ORDER BY occurred_at DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
        self.assertIsNotNone(audit)
        self.assertEqual(audit["actor"], "operator")

    def test_reclassification_drains_all_rows_with_small_batches(self) -> None:
        events = EventStore(self.database)
        for index in range(3):
            events.append(
                self.subject_id,
                "sleep_reflection",
                "sleep",
                {"index": index},
            )
        store = TrainingStore(self.database)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE training_policies SET include_private_psychology = 1, "
                "policy_version = policy_version + 1, updated_at = ? WHERE subject_id = ?",
                ("2026-08-14T00:00:00+00:00", self.subject_id),
            )
        self.assertEqual(store.reclassify_all(self.subject_id, batch_size=1), 3)
        records = store.list_records(self.subject_id, limit=10)
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record.eligibility == "eligible" for record in records))
        self.assertTrue(all(record.consent_version == 2 for record in records))

    def test_training_export_contains_eligible_events_only(self) -> None:
        events = EventStore(self.database)
        events.append(
            self.subject_id,
            "public_observation",
            "test",
            {"value": 1},
            privacy_level="public",
        )
        events.append(
            self.subject_id,
            "sleep_reflection",
            "sleep",
            {"private": "not for training"},
        )
        artifact = TrainingDatasetExporter(self.database).export(self.subject_id, actor="test")
        import zipfile
        from io import BytesIO

        with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
            data = archive.read("events.jsonl").decode()
            names = archive.namelist()
        self.assertIn("public_observation", data)
        self.assertNotIn("sleep_reflection", data)
        self.assertIn("episodes.jsonl", names)
        self.assertIn("trajectories.jsonl", names)
        self.assertIn("quality.json", names)

    def test_training_export_includes_opt_in_redacted_model_io(self) -> None:
        TrainingStore(self.database).update_policy(self.subject_id, include_model_io=True)
        ModelLedger(self.database).prepare_call(
            self.subject_id,
            "fake",
            "test-model",
            "training-test",
            "request-hash",
            "training-model-io",
            request={
                "messages": [{"content": "email person@example.com key sk-abcdefghijklmnop"}],
                "api_key": "never-export-this",
            },
        )
        artifact = TrainingDatasetExporter(self.database).export(self.subject_id, actor="test")
        import zipfile
        from io import BytesIO

        with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
            data = archive.read("model_io.jsonl").decode()
        self.assertIn("[REDACTED]", data)
        self.assertNotIn("person@example.com", data)
        self.assertNotIn("never-export-this", data)

    def test_training_export_path_writes_streamed_archive(self) -> None:
        EventStore(self.database).append(
            self.subject_id,
            "public_observation",
            "test",
            {"value": 1},
            privacy_level="public",
        )
        target = self.root / "exports" / "training.zip"
        artifact = TrainingDatasetExporter(self.database).export_to_path(
            self.subject_id,
            actor="test",
            target=target,
        )
        self.assertTrue(target.is_file())
        self.assertEqual(artifact.content, b"")
        self.assertEqual(artifact.sha256, hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertFalse(list(target.parent.glob("*.tmp")))

    def test_training_export_path_streams_past_legacy_row_bound(self) -> None:
        events = EventStore(self.database)
        for index in range(2):
            events.append(
                self.subject_id,
                "public_observation",
                "test",
                {"value": index},
                privacy_level="public",
            )
        exporter = TrainingDatasetExporter(self.database)
        exporter.max_rows = 1
        target = self.root / "exports" / "large-training.zip"
        artifact = exporter.export_to_path(self.subject_id, actor="test", target=target)
        self.assertEqual(artifact.row_count, 2)
        import zipfile

        with zipfile.ZipFile(target) as archive:
            manifest = archive.read("manifest.json").decode("utf-8")
            self.assertIn('"row_count": 2', manifest)
            self.assertEqual(len(archive.read("events.jsonl").splitlines()), 2)

    def test_training_export_workspace_is_subject_scoped_and_manifested(self) -> None:
        workspace_root = self.root / "workspace"
        own_root = workspace_root / self.subject_storage_key
        other_root = workspace_root / "Noyra-other-subject"
        own_root.mkdir(parents=True)
        other_root.mkdir(parents=True)
        (own_root / "notes.txt").write_text("subject-owned", encoding="utf-8")
        (other_root / "private.txt").write_text("other-subject-secret", encoding="utf-8")
        TrainingStore(self.database).update_policy(self.subject_id, include_workspace=True)

        artifact = TrainingDatasetExporter(self.database, workspace_root=workspace_root).export(
            self.subject_id, actor="test"
        )

        with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            notes = archive.read("workspace/notes.txt").decode("utf-8")
        self.assertIn("workspace/notes.txt", names)
        self.assertNotIn("workspace/private.txt", names)
        self.assertNotIn("other-subject-secret", notes)
        self.assertEqual(
            manifest["workspace_path_policy"],
            {
                "subject_id": self.subject_id,
                "subject_storage_key": self.subject_storage_key,
                "source_root": "workspace/<subject_storage_key>",
                "archive_root": "workspace/",
                "follow_symlinks": False,
                "reject_reparse_points": True,
                "rename_policy": "identity-and-containment-recheck",
            },
        )

    def test_training_export_workspace_rejects_escape_and_symlink_bytes(self) -> None:
        workspace_root = self.root / "workspace"
        own_root = workspace_root / self.subject_storage_key
        other_root = workspace_root / "Noyra-other-subject"
        own_root.mkdir(parents=True)
        other_root.mkdir(parents=True)
        secret = other_root / "private.txt"
        secret.write_text("other-subject-secret", encoding="utf-8")
        with self.assertRaises(ValueError):
            TrainingDatasetExporter(
                self.database, workspace_root=workspace_root
            )._subject_workspace_root("../Noyra-other-subject")

        link = own_root / "linked.txt"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are unavailable on this platform")
        TrainingStore(self.database).update_policy(self.subject_id, include_workspace=True)
        artifact = TrainingDatasetExporter(self.database, workspace_root=workspace_root).export(
            self.subject_id, actor="test"
        )
        with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
            names = set(archive.namelist())
            contents = b"".join(
                archive.read(name) for name in names if name.startswith("workspace/")
            )
        self.assertNotIn("workspace/linked.txt", names)
        self.assertNotIn(b"other-subject-secret", contents)


if __name__ == "__main__":
    unittest.main()
