from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.status_path = self.root / "root-owned" / "status.json"
        self.request = self.root / "requests" / "pending.json"
        self.current = self.root / "opt" / "current"
        release = self.root / "opt" / "releases" / "github-4d988c5-20261001022257"
        release.mkdir(parents=True)
        self.current.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.current.symlink_to(release, target_is_directory=True)
        except OSError:
            self.current = release
        self.trigger = self.root / "noyra-upgrade.path"
        self.trigger.write_text("installed", encoding="utf-8")
        self.latest_sha = "a" * 40
        self.manager = UpgradeManager(
            source_path=self.repo,
            current_release_path=self.current,
            status_path=self.status_path,
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

    def test_start_allows_the_runner_to_create_its_first_source_checkout(self) -> None:
        self.manager.source_path = self.root / "not-yet-cloned-source"
        self.manager.check_version()

        started = self.manager.start(reason="first upgrade", idempotency_key="request-1")

        self.assertEqual(started["status"], "queued")
        self.assertTrue(self.request.is_file())

    def test_git_commands_explicitly_trust_the_root_owned_upgrade_source(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("noyra.core.upgrade.subprocess.run", return_value=completed) as run:
            self.manager._git("status", "--porcelain", "--untracked-files=all")

        command = run.call_args.args[0]
        self.assertIn(f"safe.directory={self.repo.resolve()}", command)

    def test_version_check_returns_bounded_safe_projection(self) -> None:
        result = self.manager.check_version()
        self.assertEqual(result["latest"]["sha"], self.latest_sha)
        self.assertEqual(result["latest"]["short_sha"], self.latest_sha[:12])
        self.assertTrue(result["update_available"])
        self.assertNotIn("token", json.dumps(result).lower())

    def test_status_is_persisted_and_reloaded(self) -> None:
        self.manager.check_version()
        self.manager.start(reason="routine update", idempotency_key="request-1")
        self.request.unlink()
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        expected: dict[str, object] = {
            "task_id": "from-runner",
            "status": "completed",
            "phase": "complete",
            "started_at": "2026-10-01T00:00:00Z",
            "ended_at": "2026-10-01T00:01:00Z",
            "target_sha": self.latest_sha,
            "release": "e" * 40,
            "logs": [],
            "error_code": None,
        }
        self.status_path.write_text(json.dumps(expected), encoding="utf-8")
        status = UpgradeManager(
            source_path=self.repo,
            current_release_path=self.current,
            status_path=self.status_path,
            request_path=self.request,
            runner_trigger_path=self.trigger,
            github_owner="example",
            github_repo="noyra",
        ).status()
        self.assertEqual(status["task_id"], "from-runner")
        self.assertEqual(status["status"], "completed")

    def test_start_is_idempotent_for_the_same_request(self) -> None:
        self.manager.check_version()
        first = self.manager.start(reason="routine update", idempotency_key="request-1")
        second = self.manager.start(reason="routine update", idempotency_key="request-1")
        self.assertEqual(first["task_id"], second["task_id"])
        request = json.loads(self.request.read_text(encoding="utf-8"))
        self.assertEqual(request["target_sha"], self.latest_sha)

    def test_status_reports_queued_request_before_root_runner_starts(self) -> None:
        self.manager.check_version()
        started = self.manager.start(reason="routine update", idempotency_key="request-1")
        status = self.manager.status()
        self.assertEqual(status["task_id"], started["task_id"])
        self.assertEqual(status["status"], "queued")
        self.assertEqual(status["target_sha"], self.latest_sha)

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
        self.assertFalse(self.status_path.exists())
        request = self.request.read_text(encoding="utf-8")
        self.assertNotIn("super-secret", request)
        self.assertNotIn("ghp_12345678901234567890", json.dumps(self.manager.check_version()))
        self.assertLess(len(request), 4096)

    def test_start_writes_only_request_and_leaves_root_status_untouched(self) -> None:
        self.manager.check_version()
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        original = b'{"status":"runner-owned"}'
        self.status_path.write_bytes(original)
        self.manager.start(reason="routine update", idempotency_key="request-1")
        self.assertEqual(self.status_path.read_bytes(), original)
        self.assertEqual(
            {path.name for path in self.status_path.parent.iterdir()},
            {"manager.lock", "status.json"},
        )
        self.assertEqual(
            {path.name for path in self.request.parent.iterdir()},
            {"pending.json"},
        )

    def test_release_projection_uses_current_symlink_target_without_git_metadata(self) -> None:
        if not self.current.is_symlink():
            self.skipTest("symlink creation is unavailable on this Windows host")
        response = self.manager.check_version()
        self.assertEqual(response["current_release"], "github-4d988c5-20261001022257")
        self.assertIsNone(response["current_sha"])
        self.assertTrue(response["update_available"])
        self.assertFalse((self.current.resolve() / ".git").exists())

    def test_release_projection_reads_source_sha_marker_when_present(self) -> None:
        marker = self.current.resolve() / ".noyra-source-sha"
        marker.write_bytes((self.latest_sha + "\n").encode("ascii"))
        response = self.manager.check_version()
        self.assertEqual(response["current_release"], "github-4d988c5-20261001022257")
        self.assertEqual(response["current_sha"], self.latest_sha)
        self.assertFalse(response["update_available"])

    def test_different_pending_request_is_rejected_until_runner_consumes_it(self) -> None:
        self.manager.check_version()
        self.manager.start(reason="routine update", idempotency_key="request-1")
        with self.assertRaises(UpgradeError) as error:
            self.manager.start(reason="routine update", idempotency_key="request-2")
        self.assertEqual(error.exception.code, "upgrade_in_progress")

    def test_idempotent_status_projection_redacts_root_runner_log_text(self) -> None:
        self.manager.check_version()
        started = self.manager.start(reason="routine update", idempotency_key="request-1")
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(
            json.dumps(
                {
                    "task_id": started["task_id"],
                    "status": "running",
                    "phase": "install",
                    "target_sha": self.latest_sha,
                    "logs": ["token=super-secret"],
                }
            ),
            encoding="utf-8",
        )
        repeated = self.manager.start(reason="routine update", idempotency_key="request-1")
        self.assertNotIn("super-secret", json.dumps(repeated))
        self.assertEqual(repeated["logs"], ["token=[REDACTED]"])

    def test_manager_is_unavailable_without_installed_runner_trigger(self) -> None:
        self.trigger.unlink()
        with self.assertRaises(UpgradeError) as error:
            self.manager.check_version()
        self.assertEqual(error.exception.code, "upgrade_unavailable")


if __name__ == "__main__":
    unittest.main()
