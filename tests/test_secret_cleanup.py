from __future__ import annotations

import tempfile
from pathlib import Path

from noyra.core import Database, IdentityStore, SecretCleanupQueue
from noyra.core.types import content_hash


def test_secret_cleanup_queue_retries_revoked_reference_without_secret_content() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = Database(root / "noyra.sqlite3")
        subject_id = "Noyra-secret-cleanup"
        IdentityStore(database).ensure(subject_id, content_hash({"seed": "cleanup"}))
        secret_dir = root / "secrets"
        secret_dir.mkdir()
        reference = "revoked.key"
        (secret_dir / reference).write_text("secret-value", encoding="utf-8")
        queue = SecretCleanupQueue(database)
        queue.enqueue(
            subject_id,
            "search",
            "resource-1",
            reference,
            OSError("temporary delete failure"),
        )
        assert queue.pending(subject_id) == 1
        assert queue.repair(subject_id, "search", secret_dir) == 1
        assert not (secret_dir / reference).exists()
        assert queue.pending(subject_id) == 0
