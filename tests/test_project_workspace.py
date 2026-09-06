from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from noyra.cognition import ProjectExecutionError, ProjectWorkspace


class ProjectWorkspaceTestCase(unittest.TestCase):
    def test_write_is_confined_and_quota_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = ProjectWorkspace(
                Path(directory), "subject_00000000000000000000000000000001", quota_bytes=8
            )
            target = workspace.write("project-1", "notes/result.txt", b"result")
            self.assertTrue(target.is_file())
            self.assertEqual(workspace.usage("project-1"), 6)
            with self.assertRaises(ProjectExecutionError):
                workspace.write("project-1", "../escape", b"bad")
            with self.assertRaises(ProjectExecutionError):
                workspace.write("project-1", "more.txt", b"more")
