"""Operator-facing, non-privileged upgrade request coordination."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from noyra.core.errors import RuntimeOwnershipError
from noyra.core.locking import ProcessLock
from noyra.core.redaction import redact_text

UPGRADE_ROOT = Path("/var/lib/noyra/upgrade")
UPGRADE_REQUEST_PATH = UPGRADE_ROOT / "requests" / "pending.json"
UPGRADE_SOURCE_PATH = Path("/opt/noyra/upgrade/source")
UPGRADE_CURRENT_RELEASE_PATH = Path("/opt/noyra/current")
UPGRADE_STATUS_PATH = UPGRADE_ROOT / "status.json"
UPGRADE_RUNNER_TRIGGER_PATH = Path("/etc/systemd/system/noyra-upgrade.path")
_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}\Z")
_ACTIVE_STATES = frozenset({"queued", "running", "rolling_back"})


class UpgradeError(RuntimeError):
    """A stable, safe error produced by an upgrade boundary."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class UpgradeManager:
    """Check a remote GitHub ref and submit a fixed SHA request for root's runner."""

    def __init__(
        self,
        *,
        source_path: Path | str = UPGRADE_SOURCE_PATH,
        current_release_path: Path | str = UPGRADE_CURRENT_RELEASE_PATH,
        status_path: Path | str = UPGRADE_STATUS_PATH,
        request_path: Path | str = UPGRADE_REQUEST_PATH,
        runner_trigger_path: Path | str = UPGRADE_RUNNER_TRIGGER_PATH,
        github_owner: str,
        github_repo: str,
        github_branch: str = "main",
        github_fetcher: Callable[[], Any] | None = None,
        check_ttl_seconds: int = 900,
    ):
        self.source_path = Path(source_path)
        self.current_release_path = Path(current_release_path)
        self.status_path = Path(status_path)
        self.request_path = Path(request_path)
        self.runner_trigger_path = Path(runner_trigger_path)
        self.github_owner = self._bounded_name(github_owner)
        self.github_repo = self._bounded_name(github_repo)
        self.github_branch = self._bounded_name(github_branch)
        self.github_fetcher = github_fetcher or self._fetch_github_metadata
        self.check_ttl = timedelta(seconds=max(30, min(int(check_ttl_seconds), 3600)))
        # The installer's state directory is root-owned and shared with the
        # privileged runner. Keeping this lock outside the app-writable request
        # directory lets both sides serialize the request handoff safely.
        self._lock = ProcessLock(self.status_path.parent / "manager.lock")
        self._thread_lock = threading.RLock()
        self._checked: tuple[str, datetime] | None = None

    @staticmethod
    def _bounded_name(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
            raise ValueError("upgrade repository configuration is invalid")
        return value

    def _require_available(self) -> None:
        if not self.runner_trigger_path.is_file():
            raise UpgradeError("upgrade_unavailable")

    @contextmanager
    def _process_guard(self) -> Iterator[None]:
        with self._thread_lock:
            try:
                self._lock.acquire()
            except (OSError, RuntimeOwnershipError):
                raise UpgradeError("upgrade_in_progress") from None
            try:
                yield
            finally:
                self._lock.release()

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        try:
            repository = (cwd or self.source_path).resolve(strict=True)
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    f"safe.directory={repository}",
                    "-C",
                    str(repository),
                    *args,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            raise UpgradeError("upgrade_start_failed") from None
        return result.stdout.strip()

    def _current_release(self) -> str:
        try:
            release_path = self.current_release_path.resolve(strict=True)
        except OSError:
            raise UpgradeError("upgrade_start_failed") from None
        if not release_path.is_dir():
            raise UpgradeError("upgrade_start_failed")
        value = release_path.name
        if not _RELEASE_ID.fullmatch(value) or value in {".", ".."}:
            raise UpgradeError("upgrade_start_failed")
        return value

    def _current_sha(self) -> str | None:
        try:
            release_path = self.current_release_path.resolve(strict=True)
            with (release_path / ".noyra-source-sha").open("rb") as stream:
                raw = stream.read(66)
        except FileNotFoundError:
            return None
        except OSError:
            raise UpgradeError("upgrade_unavailable") from None
        if len(raw) > 65:
            raise UpgradeError("upgrade_unavailable")
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError:
            raise UpgradeError("upgrade_unavailable") from None
        if value.endswith("\n"):
            value = value[:-1]
        if not _SHA.fullmatch(value):
            raise UpgradeError("upgrade_unavailable")
        return value

    def _require_clean_source(self) -> None:
        if self.source_path.is_symlink():
            raise UpgradeError("upgrade_start_failed")
        if not self.source_path.exists():
            # The privileged runner creates this fixed official checkout on
            # the first upgrade; the application must not create root-owned
            # source files itself.
            return
        if not self.source_path.is_dir():
            raise UpgradeError("upgrade_start_failed")
        if self._git("status", "--porcelain", "--untracked-files=all"):
            raise UpgradeError("upgrade_source_dirty")

    def _fetch_github_metadata(self) -> Any:
        url = (
            f"https://api.github.com/repos/{self.github_owner}/{self.github_repo}"
            f"/commits/{self.github_branch}"
        )
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Noyra-upgrade-check"},
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                raw = response.read(65_537)
            if len(raw) > 65_536:
                raise ValueError("response too large")
            return json.loads(raw)
        except (OSError, urllib.error.URLError, ValueError, TimeoutError):
            raise UpgradeError("upgrade_unavailable") from None

    def _parse_metadata(self, payload: Any) -> dict[str, str]:
        if isinstance(payload, dict) and set(payload).issuperset({"sha", "committed_at", "title"}):
            sha = payload.get("sha")
            committed_at = payload.get("committed_at")
            title = payload.get("title")
        else:
            try:
                sha = payload["sha"]
                commit = payload["commit"]
                committed_at = commit["committer"]["date"]
                title = commit["message"].splitlines()[0]
            except (KeyError, TypeError, IndexError, AttributeError):
                raise UpgradeError("upgrade_unavailable") from None
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise UpgradeError("upgrade_unavailable")
        if not isinstance(committed_at, str) or len(committed_at) > 40:
            raise UpgradeError("upgrade_unavailable")
        try:
            datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
        except ValueError:
            raise UpgradeError("upgrade_unavailable") from None
        if not isinstance(title, str):
            raise UpgradeError("upgrade_unavailable")
        title_lines = title.splitlines()
        safe_title = redact_text(title_lines[0] if title_lines else "No commit message")[:160]
        return {"sha": sha, "committed_at": committed_at, "title": safe_title}

    def check_version(self) -> dict[str, Any]:
        self._require_available()
        current = self._current_release()
        current_sha = self._current_sha()
        latest = self._parse_metadata(self.github_fetcher())
        checked_at = datetime.now(UTC)
        self._checked = (latest["sha"], checked_at)
        return {
            "current_release": current,
            "current_sha": current_sha,
            "latest": {
                **latest,
                "short_sha": latest["sha"][:12],
            },
            "checked_at": checked_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "update_available": current_sha is None or latest["sha"] != current_sha,
        }

    def status(self) -> dict[str, Any]:
        self._require_available()
        pending = self._read_pending_unlocked()
        if pending is not None:
            current = self._read_status_unlocked()
            if current is None or current.get("status") not in _ACTIVE_STATES:
                task_id = pending.get("task_id")
                target_sha = pending.get("target_sha")
                requested_at = pending.get("requested_at")
                if (
                    not isinstance(task_id, str)
                    or not re.fullmatch(r"[a-f0-9]{32}", task_id)
                    or not isinstance(target_sha, str)
                    or not _SHA.fullmatch(target_sha)
                    or not isinstance(requested_at, str)
                    or len(requested_at) > 40
                ):
                    raise UpgradeError("upgrade_unavailable")
                return self._public_status(
                    {
                        "task_id": task_id,
                        "status": "queued",
                        "phase": "queued",
                        "started_at": requested_at,
                        "target_sha": target_sha,
                        "logs": [],
                    }
                )
        try:
            with self.status_path.open("rb") as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError("status is too large")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("status is invalid")
        except FileNotFoundError:
            return {
                "task_id": None,
                "status": "idle",
                "phase": "idle",
                "started_at": None,
                "ended_at": None,
                "target_sha": None,
                "release": None,
                "logs": [],
                "error_code": None,
            }
        except (OSError, ValueError, json.JSONDecodeError):
            raise UpgradeError("upgrade_unavailable") from None
        fields = (
            "task_id",
            "status",
            "phase",
            "started_at",
            "ended_at",
            "target_sha",
            "release",
            "logs",
            "error_code",
        )
        result: dict[str, Any] = {field: payload.get(field) for field in fields}
        raw_logs = result["logs"]
        if not isinstance(raw_logs, list):
            raw_logs = []
        result["logs"] = [
            redact_text(line[:240]) for line in raw_logs[:20] if isinstance(line, str)
        ]
        for field, maximum in (("task_id", 64), ("status", 32), ("phase", 32), ("error_code", 64)):
            value = result[field]
            result[field] = redact_text(value[:maximum]) if isinstance(value, str) else None
        for field in ("started_at", "ended_at"):
            value = result[field]
            result[field] = redact_text(value[:40]) if isinstance(value, str) else None
        for field in ("target_sha", "release"):
            value = result[field]
            result[field] = value if isinstance(value, str) and _SHA.fullmatch(value) else None
        return result

    def start(
        self,
        *,
        reason: str,
        idempotency_key: str,
        target_sha: str | None = None,
    ) -> dict[str, Any]:
        self._require_available()
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 512
            or not isinstance(idempotency_key, str)
            or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key)
        ):
            raise UpgradeError("upgrade_target_invalid")
        checked = self._checked
        if checked is None or datetime.now(UTC) - checked[1] > self.check_ttl:
            raise UpgradeError("upgrade_target_invalid")
        requested_sha = checked[0] if target_sha is None else target_sha
        if (
            not isinstance(requested_sha, str)
            or not _SHA.fullmatch(requested_sha)
            or requested_sha != checked[0]
        ):
            raise UpgradeError("upgrade_target_invalid")
        self._require_clean_source()
        with self._process_guard():
            key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            task_id = hashlib.sha256(f"{requested_sha}:{key_hash}".encode()).hexdigest()[:32]
            current = self._read_status_unlocked()
            if current and current.get("task_id") == task_id:
                return self._public_status(current)
            pending = self._read_pending_unlocked()
            if pending and pending.get("task_id") == task_id:
                return self._public_status(
                    {
                        "task_id": task_id,
                        "status": "queued",
                        "phase": "queued",
                        "started_at": pending.get("requested_at"),
                        "target_sha": pending.get("target_sha"),
                    }
                )
            if pending:
                raise UpgradeError("upgrade_in_progress")
            if current and current.get("status") in _ACTIVE_STATES:
                raise UpgradeError("upgrade_in_progress")
            now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            request: dict[str, Any] = {
                "task_id": task_id,
                "target_sha": requested_sha,
                "requested_at": now,
            }
            try:
                self._atomic_json(self.request_path, request)
            except OSError:
                raise UpgradeError("upgrade_start_failed") from None
            return self._public_status(
                {
                    "task_id": task_id,
                    "status": "queued",
                    "phase": "queued",
                    "started_at": now,
                    "ended_at": None,
                    "target_sha": requested_sha,
                    "release": None,
                    "logs": [],
                    "error_code": None,
                }
            )

    def _read_status_unlocked(self) -> dict[str, Any] | None:
        try:
            with self.status_path.open("rb") as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                raise UpgradeError("upgrade_unavailable")
            value = json.loads(raw)
        except FileNotFoundError:
            return None
        except UpgradeError:
            raise
        except (OSError, ValueError):
            raise UpgradeError("upgrade_unavailable") from None
        if not isinstance(value, dict):
            raise UpgradeError("upgrade_unavailable")
        return value

    def _read_pending_unlocked(self) -> dict[str, Any] | None:
        try:
            with self.request_path.open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise UpgradeError("upgrade_unavailable")
            value = json.loads(raw)
        except FileNotFoundError:
            return None
        except UpgradeError:
            raise
        except (OSError, ValueError):
            raise UpgradeError("upgrade_unavailable") from None
        if not isinstance(value, dict):
            raise UpgradeError("upgrade_unavailable")
        return value

    @staticmethod
    def _public_status(value: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: value.get(key)
            for key in (
                "task_id",
                "status",
                "phase",
                "started_at",
                "ended_at",
                "target_sha",
                "release",
                "logs",
                "error_code",
            )
        }
        for field, maximum in (
            ("task_id", 64),
            ("status", 32),
            ("phase", 32),
            ("error_code", 64),
            ("started_at", 40),
            ("ended_at", 40),
        ):
            item = result[field]
            result[field] = redact_text(item[:maximum]) if isinstance(item, str) else None
        for field in ("target_sha", "release"):
            item = result[field]
            result[field] = item if isinstance(item, str) and _SHA.fullmatch(item) else None
        logs = result["logs"]
        result["logs"] = (
            [redact_text(line[:240]) for line in logs[:20] if isinstance(line, str)]
            if isinstance(logs, list)
            else []
        )
        return result

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 4096:
            raise OSError("upgrade document exceeds its bound")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
