from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from noyra.core import IdentityStore, SnapshotStore
from noyra.core.database import Database


class SnapshotCompactionTestCase(unittest.TestCase):
    def test_old_snapshots_compress_and_latest_remains_online(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "noyra.sqlite3")
            subject_id = "Noyra-snapshot-test"
            IdentityStore(database).ensure(subject_id, "a" * 64)
            store = SnapshotStore(database)
            for version in range(1, 21):
                store.save(subject_id, {"version": version}, state_version=version, reason="test")
            result = store.compact(subject_id, keep_recent=5)
            self.assertEqual(result["archived"], 15)
            self.assertEqual(store.latest(subject_id).state_version, 20)
            self.assertEqual(store.verify_archives(subject_id), 1)
