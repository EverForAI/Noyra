from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from noyra.core.upgrade import UpgradeError, UpgradeManager


class UpgradeManagerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "current"], check=True)
        self.state = self.root / "state"
        self.request = self.root / "requests" / "pending.json"
        self.trigger = self.root / "noyra-upgrade.path"
        self.trigger.write_text("installed", encoding="utf-8")
        self.latest_sha = "a" * 40
        self.manager = UpgradeManager(
            repo_path=self.repo,
            state_dir=self.state,
            request_path=self.request,
            runner_trigger_path=self.trigger,
            github_owner="example",
            github_repo="noyra",
            github_fetcher=lambda: {
                "sha": self.latest_sha,
                "committed_at": "2026-10-01T00:00:00Z",
                "title": "A safe release",
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_start_rejects_dirty_source_tree(self) -> None:
        self.manager.check_version()
        (self.repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
        with self.assertRaises(UpgradeError) as error:
            self.manager.start(reason="routine update", idempotency_key="request-1")
        self.assertEqual(error.exception.code, "upgrade_source_dirty")
        self.assertFalse(self.request.exists())

    def test_version_check_returns_bounded_safe_projection(self) -> None:
        result = self.manager.check_version()
        self.assertEqual(result["latest"]["sha"], self.latest_sha)
        self.assertEqual(result["latest"]["short_sha"], self.latest_sha[:12])
        self.assertTrue(result["update_available"])
        self.assertNotIn("token", json.dumps(result).lower())

    def test_status_is_persisted_and_reloaded(self) -> None:
        self.manager.check_version()
        started = self.manager.start(reason="routine update", idempotency_key="request-1")
        status = UpgradeManager(
            repo_path=self.repo,
            state_dir=self.state,
            request_path=self.request,
            runner_trigger_path=self.trigger,
            github_owner="example",
            github_repo="noyra",
        ).status()
        self.assertEqual(status["task_id"], started["task_id"])
        self.assertEqual(status["target_sha"], self.latest_sha)

    def test_start_is_idempotent_for_the_same_request(self) -> None:
        self.manager.check_version()
        first = self.manager.start(reason="routine update", idempotency_key="request-1")
        second = self.manager.start(reason="routine update", idempotency_key="request-1")
        self.assertEqual(first["task_id"], second["task_id"])
        request = json.loads(self.request.read_text(encoding="utf-8"))
        self.assertEqual(request["target_sha"], self.latest_sha)

    def test_target_must_be_from_a_recent_successful_check(self) -> None:
        with self.assertRaises(UpgradeError) as error:
            self.manager.start(
                target_sha=self.latest_sha,
                reason="routine",
                idempotency_key="request-1",
            )
        self.assertEqual(error.exception.code, "upgrade_target_invalid")

    def test_status_and_request_redact_secrets_and_bound_free_text(self) -> None:
        self.manager.github_fetcher = lambda: {
            "sha": self.latest_sha,
            "committed_at": "2026-10-01T00:00:00Z",
            "title": "release ghp_12345678901234567890",
        }
        self.manager.check_version()
        self.manager.start(
            reason="token=super-secret " + "x" * 100,
            idempotency_key="request-1",
        )
        persisted = self.state.joinpath("status.json").read_text(encoding="utf-8")
        request = self.request.read_text(encoding="utf-8")
        self.assertNotIn("super-secret", persisted + request)
        self.assertNotIn("ghp_12345678901234567890", persisted)
        self.assertLess(len(persisted), 8192)
        self.assertLess(len(request), 4096)

    def test_manager_is_unavailable_without_installed_runner_trigger(self) -> None:
        self.trigger.unlink()
        with self.assertRaises(UpgradeError) as error:
            self.manager.check_version()
        self.assertEqual(error.exception.code, "upgrade_unavailable")


if __name__ == "__main__":
    unittest.main()
