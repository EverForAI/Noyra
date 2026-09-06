from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from noyra.core.archive import LocalArchiveProvider, StorageQuota, StorageUsageScanner


class ArchiveTestCase(unittest.TestCase):
    def test_local_provider_is_atomic_and_confined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = LocalArchiveProvider(directory, encryption_key=b"k" * 32)
            digest = provider.put("2026/events.bin", b"events")
            self.assertTrue(provider.exists("2026/events.bin"))
            self.assertEqual(provider.get("2026/events.bin"), b"events")
            self.assertNotIn(b"events", (Path(directory) / "2026" / "events.bin").read_bytes())
            self.assertEqual(len(digest), 64)
            with self.assertRaises(ValueError):
                provider.put("../escape", b"bad")

    def test_usage_scanner_separates_workspace_from_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "subject").mkdir()
            (root / "training_raw").mkdir()
            (root / "workspace").mkdir()
            (root / "exports").mkdir()
            (root / "secrets").mkdir()
            (root / "subject" / "state.db").write_bytes(b"123")
            (root / "exports" / "old.zip").write_bytes(b"12")
            (root / "secrets" / "key").write_bytes(b"1")
            (root / "workspace" / "project.bin").write_bytes(b"12345")
            usage = StorageUsageScanner(root).scan()
            self.assertEqual(usage.subject_bytes, 6)
            self.assertEqual(usage.workspace_bytes, 5)
            self.assertEqual(usage.over_quota(StorageQuota(subject_bytes=2)), ("subject",))


if __name__ == "__main__":
    unittest.main()
