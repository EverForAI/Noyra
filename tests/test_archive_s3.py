from __future__ import annotations

import base64
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from noyra.core import (
    ArchiveRecovery,
    ArchiveTransferQueue,
    CloudArchiveCoordinator,
    Database,
    EventStore,
    IdentityStore,
    StorageLayout,
    StorageLifecycleManager,
    StorageQuota,
)
from noyra.core.archive import S3ArchiveProvider


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}

    def put_object(self, **kwargs: object) -> None:
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = (
            kwargs["Body"] if isinstance(kwargs["Body"], bytes) else b"",
            kwargs["Metadata"] if isinstance(kwargs["Metadata"], dict) else {},
        )

    def get_object(self, **kwargs: object) -> dict[str, object]:
        payload, metadata = self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))]
        return {"Body": io.BytesIO(payload), "Metadata": metadata}

    def head_object(self, **kwargs: object) -> dict[str, object]:
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))]
        return {}


class MemoryArchiveProvider:
    name = "memory"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, object_key: str, payload: bytes) -> str:
        self.objects[object_key] = payload
        return hashlib.sha256(payload).hexdigest()

    def get(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        del max_bytes
        return self.objects[object_key]

    def exists(self, object_key: str) -> bool:
        return object_key in self.objects


class S3ArchiveProviderTestCase(unittest.TestCase):
    def test_put_get_exists_and_prefix_boundary(self) -> None:
        provider = S3ArchiveProvider(FakeS3(), bucket="noyra", prefix="subject-a")
        digest = provider.put("cold/state.bin", b"state")
        self.assertEqual(len(digest), 64)
        self.assertTrue(provider.exists("cold/state.bin"))
        self.assertEqual(provider.get("cold/state.bin"), b"state")
        with self.assertRaises(ValueError):
            provider.put("../escape", b"bad")

    def test_queue_retries_and_uploads_checksum_verified_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "noyra.sqlite3")
            from noyra.core import IdentityStore

            IdentityStore(database).ensure("Noyra-archive-test", "a" * 64)
            queue = ArchiveTransferQueue(database, "Noyra-archive-test", directory)
            queue.enqueue("cold/state.bin", b"state")
            provider = S3ArchiveProvider(FakeS3(), bucket="noyra")
            self.assertEqual(queue.drain(provider), {"uploaded": 1, "failed": 0, "dead": 0})

    def test_restore_is_checksum_verified_and_confined(self) -> None:
        client = FakeS3()
        provider = S3ArchiveProvider(client, bucket="noyra")
        digest = provider.put("cold/state.bin", b"state")
        with tempfile.TemporaryDirectory() as directory:
            restored = ArchiveRecovery.restore(
                provider,
                "cold/state.bin",
                directory,
                "restored/state.bin",
                expected_hash=digest,
            )
            self.assertEqual(restored.read_bytes(), b"state")
            with self.assertRaises(ValueError):
                ArchiveRecovery.restore(
                    provider, "cold/state.bin", directory, "../escape", expected_hash=digest
                )

    def test_cloud_coordinator_queues_encrypted_event_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = StorageLayout.create(root)
            database = Database(root / "noyra.sqlite3")
            subject_id = "Noyra-cloud-event-test"
            IdentityStore(database).ensure(subject_id, "b" * 64)
            old = EventStore(database).append(
                subject_id,
                "old_experience",
                "test",
                {"secret": "payload"},
                occurred_at="2020-01-01T00:00:00+00:00",
            )
            key = base64.urlsafe_b64encode(b"k" * 32).decode()
            with patch.dict(os.environ, {"NOYRA_ARCHIVE_ENCRYPTION_KEY": key}):
                StorageLifecycleManager(
                    database,
                    subject_id,
                    layout,
                    StorageQuota(),
                    event_payload_retention_days=30,
                    minimum_free_bytes=10_000_000,
                ).maintain()
                provider = MemoryArchiveProvider()
                coordinator = CloudArchiveCoordinator(
                    database,
                    subject_id,
                    layout.training_raw,
                    provider,
                    local_archive_root=layout.subject / "cold",
                )
                result = coordinator.tick()
            self.assertEqual(result["uploaded"], 1)
            self.assertEqual(len(provider.objects), 1)
            self.assertNotIn(b'"secret"', next(iter(provider.objects.values())))
            with database.connection() as connection:
                status = connection.execute(
                    "SELECT status FROM storage_archives WHERE subject_id = ? "
                    "AND object_key LIKE 'events/%'",
                    (subject_id,),
                ).fetchone()[0]
            self.assertEqual(status, "verified")
            self.assertIsNotNone(old.event_id)


if __name__ == "__main__":
    unittest.main()
