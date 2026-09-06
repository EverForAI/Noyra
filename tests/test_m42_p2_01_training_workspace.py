from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from noyra.core import Database, IdentityStore, TrainingStore
from noyra.core.training_export import TrainingDatasetExporter
from noyra.core.types import content_hash


class TrainingWorkspaceIsolationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "noyra.sqlite3")
        self.subject_id = "Noyra-workspace-a"
        self.other_subject_id = "Noyra-workspace-b"
        identities = IdentityStore(self.database)
        for subject_id in (self.subject_id, self.other_subject_id):
            identities.ensure(subject_id, content_hash({"subject_id": subject_id}))
        self.subject_storage_key = identities.storage_key(self.subject_id)
        self.other_subject_storage_key = identities.storage_key(self.other_subject_id)
        TrainingStore(self.database).update_policy(self.subject_id, include_workspace=True)
        self.workspace_root = self.root / "workspace"
        self.subject_root = self.workspace_root / self.subject_storage_key
        self.other_subject_root = self.workspace_root / self.other_subject_storage_key
        self.subject_root.mkdir(parents=True)
        self.other_subject_root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def export(self, name: str = "training.zip") -> Path:
        target = self.root / "exports" / name
        TrainingDatasetExporter(self.database, workspace_root=self.workspace_root).export_to_path(
            self.subject_id, actor="test", target=target
        )
        return target

    def test_export_starts_at_subject_root_and_records_exact_path_policy(self) -> None:
        (self.workspace_root / "shared.md").write_text("shared-root-secret", encoding="utf-8")
        (self.subject_root / "own.md").write_text("subject-a", encoding="utf-8")
        (self.other_subject_root / "other.md").write_text("subject-b-secret", encoding="utf-8")

        with zipfile.ZipFile(self.export()) as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            own = archive.read("workspace/own.md")

        self.assertEqual(own, b"subject-a")
        self.assertNotIn("workspace/shared.md", names)
        self.assertNotIn("workspace/other.md", names)
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
        self.assertEqual(set(manifest["workspace_files"]), {"workspace/own.md"})

    def test_foreign_directory_link_is_never_followed(self) -> None:
        (self.subject_root / "own.md").write_text("subject-a", encoding="utf-8")
        (self.other_subject_root / "other.md").write_text("subject-b-secret", encoding="utf-8")
        link = self.subject_root / "foreign"
        self._make_directory_link(link, self.other_subject_root)

        try:
            with zipfile.ZipFile(self.export("linked.zip")) as archive:
                names = set(archive.namelist())
                content = b"\n".join(archive.read(name) for name in names)
        finally:
            self._remove_directory_link(link)

        self.assertNotIn("workspace/foreign/other.md", names)
        self.assertNotIn(b"subject-b-secret", content)

    def test_subject_root_link_fails_closed(self) -> None:
        self.subject_root.rmdir()
        (self.other_subject_root / "other.md").write_text("subject-b-secret", encoding="utf-8")
        self._make_directory_link(self.subject_root, self.other_subject_root)

        try:
            with self.assertRaisesRegex(ValueError, "subject workspace root"):
                self.export("subject-link.zip")
        finally:
            self._remove_directory_link(self.subject_root)
        self.assertFalse((self.root / "exports" / "subject-link.zip").exists())

    def test_compatibility_export_preserves_workspace_root_link_rejection(self) -> None:
        linked_root = self.root / "workspace-link"
        self._make_directory_link(linked_root, self.workspace_root)
        try:
            exporter = TrainingDatasetExporter(
                self.database,
                workspace_root=linked_root,
            )
            with self.assertRaisesRegex(ValueError, "workspace root"):
                exporter.export(self.subject_id, actor="test")
        finally:
            self._remove_directory_link(linked_root)
        self.assertFalse(list((self.root / "exports" / "work").glob("*.work")))

    def test_rename_race_cannot_substitute_foreign_bytes(self) -> None:
        own = self.subject_root / "own.md"
        foreign = self.other_subject_root / "foreign.md"
        displaced = self.subject_root / ".own.md.displaced"
        own.write_text("subject-a", encoding="utf-8")
        foreign.write_text("subject-b-secret", encoding="utf-8")
        exporter = TrainingDatasetExporter(self.database, workspace_root=self.workspace_root)
        original_read = TrainingDatasetExporter._read_workspace_bytes
        raced = False

        def swap_around_read(*args: object, **kwargs: object) -> bytes | None:
            nonlocal raced
            path = Path(str(args[2]))
            if raced or path != own:
                return original_read(*args, **kwargs)  # type: ignore[arg-type]
            raced = True
            os.replace(own, displaced)
            os.replace(foreign, own)
            try:
                return original_read(*args, **kwargs)  # type: ignore[arg-type]
            finally:
                os.replace(own, foreign)
                os.replace(displaced, own)

        target = self.root / "exports" / "race.zip"
        try:
            with patch.object(
                TrainingDatasetExporter,
                "_read_workspace_bytes",
                side_effect=swap_around_read,
            ):
                exporter.export_to_path(self.subject_id, actor="test", target=target)
        except RuntimeError:
            self.assertFalse(target.exists())
            return

        self.assertTrue(raced)
        with zipfile.ZipFile(target) as archive:
            content = b"\n".join(archive.read(name) for name in archive.namelist())
        self.assertNotIn(b"subject-b-secret", content)

    def _make_directory_link(self, link: Path, target: Path) -> None:
        if os.name != "nt":
            link.symlink_to(target, target_is_directory=True)
            return
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            self.fail(f"failed to create Windows junction: {completed.stderr}")

    @staticmethod
    def _remove_directory_link(link: Path) -> None:
        if os.name == "nt" and link.exists():
            os.rmdir(link)
        else:
            link.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
