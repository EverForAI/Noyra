from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from noyra.core import Database, ExportControl, ExportJobManager, IdentityStore
from noyra.core.types import content_hash


def test_running_export_cancel_does_not_publish_completed_artifact() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = Database(root / "noyra.sqlite3")
        subject_id = "Noyra-export-cancel"
        IdentityStore(database).ensure(subject_id, content_hash({"seed": "export-cancel"}))
        started = threading.Event()
        release = threading.Event()

        def worker(
            _subject: str, _kind: str, target: Path, control: ExportControl
        ) -> tuple[str, str]:
            started.set()
            release.wait(5)
            control.checkpoint()
            target.write_bytes(b"partial")
            return "partial.zip", "hash"

        manager = ExportJobManager(database, root / "exports", worker)
        manager.start_after_ownership(subject_id)
        try:
            job = manager.create(subject_id, "runtime")
            future = manager.futures[job.job_id]
            assert started.wait(2)
            cancelled = manager.cancel(job.job_id, subject_id)
            assert cancelled.status == "cancelled"
            release.set()
            future.result(timeout=2)
            assert manager.get(job.job_id, subject_id=subject_id).status == "cancelled"
            assert not list((root / "exports").glob("*.zip"))
        finally:
            release.set()
            manager.close()
