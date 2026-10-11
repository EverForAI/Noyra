from __future__ import annotations

import json
from email.message import Message
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import EventStore, SubjectKernel
from noyra.core.types import content_hash
from noyra.core.upgrade import UpgradeError, UpgradeManager
from noyra.interaction import PublicProjection
from noyra.service import NoyraHTTPServer, ServiceSettings


def test_runtime_filters_keep_snapshot_pagination_and_boundaries(tmp_path: Path) -> None:
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", "log-filter", content_hash("logs"))
    try:
        projection = PublicProjection(kernel.database)
        events = EventStore(kernel.database)
        for second in range(8):
            events.append(
                kernel.subject_id,
                "sample",
                "runtime",
                {"private": "not a summary"},
                occurred_at=f"2026-10-09T00:00:0{second}.000+00:00",
            )
        filters: dict[str, Any] = {
            "category": "event",
            "status": "recorded",
            "since": "2026-10-09T08:00:02+08:00",
            "until": "2026-10-09T00:00:07Z",
        }
        page = projection.runtime_logs(kernel.subject_id, limit=2, **filters)
        items = list(page["items"])
        events.append(
            kernel.subject_id, "late", "runtime", {}, occurred_at="2026-10-09T00:00:05.500+00:00"
        )
        while page["next_cursor"]:
            page = projection.runtime_logs(
                kernel.subject_id,
                limit=2,
                cursor=page["next_cursor"],
                **filters,
            )
            items.extend(page["items"])
        assert len(items) == 5
        assert [row["occurred_at"] for row in items] == [
            f"2026-10-09T00:00:0{second}.000+00:00" for second in range(6, 1, -1)
        ]
        assert "not a summary" not in json.dumps(items)
        for invalid in (
            {"category": "bad"},
            {"status": "' OR 1=1"},
            {"since": "2026-10-09"},
            {"until": "not-a-date"},
        ):
            with pytest.raises(ValueError):
                projection.runtime_logs(kernel.subject_id, **cast(dict[str, Any], invalid))
    finally:
        kernel.close()


def test_operator_log_routes_require_auth_and_keep_private_content_out(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="operator-log",
        genesis_hash=content_hash("operator"),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr("test-token-sufficient-entropy-for-preview"),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    http = NoyraHTTPServer(kernel, settings)
    http.start()
    try:
        address = f"http://127.0.0.1:{http.address[1]}"
        for path in ("/api/admin/activity-logs", "/api/admin/lifecycle-summary"):
            with pytest.raises(HTTPError) as failure:
                urlopen(address + path, timeout=10)
            assert failure.value.code == 401
            with urlopen(
                Request(
                    address + path,
                    headers={
                        "Authorization": "Bearer test-token-sufficient-entropy-for-preview",
                    },
                ),
                timeout=10,
            ) as response:
                payload = json.loads(response.read())
                assert response.status == 200
            assert "trigger_reason" not in json.dumps(payload)
            assert "payload_json" not in json.dumps(payload)
            if path.endswith("lifecycle-summary"):
                assert payload["state_elapsed_seconds"] >= 0
    finally:
        http.close()
        kernel.close()


def test_installed_version_does_not_require_remote_or_runner(tmp_path: Path) -> None:
    release = tmp_path / "release-20261009"
    release.mkdir()
    (release / ".noyra-source-sha").write_text("a" * 40 + "\n", newline="\n")

    def unavailable() -> None:
        raise AssertionError("installed version must never access the remote")

    manager = UpgradeManager(
        github_owner="example",
        github_repo="noyra",
        current_release_path=release,
        runner_trigger_path=tmp_path / "missing.path",
        github_fetcher=unavailable,
    )
    assert manager.installed_version()["current_sha"] == "a" * 40


@pytest.mark.parametrize(
    ("status", "remaining", "expected"),
    [
        (404, None, "upgrade_no_stable_release"),
        (403, "0", "upgrade_remote_rate_limited"),
        (429, None, "upgrade_remote_rate_limited"),
        (503, None, "upgrade_remote_http_error"),
    ],
)
def test_release_check_reports_safe_remote_failure(
    monkeypatch: pytest.MonkeyPatch, status: int, remaining: str | None, expected: str
) -> None:
    headers = Message()
    if remaining is not None:
        headers["X-RateLimit-Remaining"] = remaining

    def fail(*args: Any) -> None:
        raise HTTPError(
            "https://api.github.com/repos/example/noyra/releases/latest",
            status,
            "redact-private-message",
            headers,
            None,
        )

    monkeypatch.setattr("noyra.core.upgrade.stable_metadata", fail)
    manager = UpgradeManager(github_owner="example", github_repo="noyra")
    with pytest.raises(UpgradeError) as failure:
        manager._fetch_github_metadata()
    assert failure.value.code == expected
