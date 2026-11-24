from __future__ import annotations

import errno
import time
from pathlib import Path
from typing import Any

import pytest

from noyra.core import at_rest
from noyra.core.at_rest import AtRestConfig, AtRestGuard


class _Keyring:
    def metadata(self) -> dict[str, Any]:
        return {"key_id": "test"}


def _ready_guard(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AtRestGuard, tuple[int, ...]]:
    guard = AtRestGuard(AtRestConfig(data_root=root, mode="required"))
    guard._prepared = True
    monkeypatch.setattr(guard, "_require_volume", lambda: None)
    monkeypatch.setattr(guard, "_require_keyring", lambda: _Keyring())
    monkeypatch.setattr(guard, "_monitor_permissions", lambda: None)
    return guard, guard._permission_root_signature()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("private permission audit exceeds its work budget", "private permission audit timed out"),
        (
            "Windows private-storage ACL check failed: "
            "Windows ACL audit timed out after 60 seconds",
            "private permission audit timed out",
        ),
        (
            "Windows private-storage ACL check failed: ACL probe busy",
            "private permission audit is busy",
        ),
        (
            "Windows private-storage ACL check failed: ACL probe unavailable: FileNotFoundError",
            "private permission audit scan failed",
        ),
        (OSError(errno.EBUSY, "resource busy"), "private permission audit is busy"),
        (RuntimeError("unexpected scan failure"), "private permission audit scan failed"),
    ],
)
def test_transient_refresh_failure_preserves_only_the_original_fresh_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | BaseException,
    expected: str,
) -> None:
    guard, signature = _ready_guard(tmp_path, monkeypatch)
    checked_at = time.monotonic()
    guard._permission_snapshot = (signature, checked_at, None)

    def fail_scan(*_args: Any, **_kwargs: Any) -> str | None:
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(at_rest, "_private_permission_error", fail_scan)
    try:
        assert (
            guard._audit_private_permissions(
                work_budget_seconds=4.0,
                wait_for_scan=False,
            )
            == expected
        )
        assert guard._permission_snapshot == (signature, checked_at, None)
        assert guard._permission_audit_failure is not None
        assert guard._permission_audit_failure[1] == expected

        # A failed refresh does not discard a proof that is still inside the
        # existing five-second window.
        assert guard.request_health()["ready"]

        # Nor can it renew or extend that proof after its original expiry.
        guard._permission_snapshot = (signature, checked_at - 6.0, None)
        expired = guard.request_health()
        assert not expired["ready"]
        assert expired["reason"] == expected
    finally:
        guard.close()


def test_windows_permission_probe_reports_filesystem_busy_as_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def busy_command(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.EBUSY, "filesystem resource is busy")

    monkeypatch.setattr(at_rest, "_powershell_command", lambda _script: ["powershell"])
    monkeypatch.setattr(at_rest, "_run_command", busy_command)
    assert at_rest._windows_permission_audit(tmp_path) == (False, "ACL probe busy")


def test_completed_unsafe_scan_immediately_replaces_a_fresh_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, signature = _ready_guard(tmp_path, monkeypatch)
    guard._permission_snapshot = (signature, time.monotonic(), None)
    monkeypatch.setattr(
        at_rest,
        "_private_permission_error",
        lambda *_args, **_kwargs: "private file is accessible by group/other: timeout-secret.bin",
    )
    try:
        result = guard._audit_private_permissions(
            work_budget_seconds=4.0,
            wait_for_scan=False,
        )
        assert result == "private file is accessible by group/other: timeout-secret.bin"
        assert guard._permission_snapshot is not None
        assert guard._permission_snapshot[2] == result
        health = guard.request_health()
        assert not health["ready"]
        assert health["reason"] == result
    finally:
        guard.close()


def test_scan_contention_is_reported_without_extending_or_erasing_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, signature = _ready_guard(tmp_path, monkeypatch)
    checked_at = time.monotonic()
    guard._permission_snapshot = (signature, checked_at, None)
    guard._permission_scan_lock.acquire()
    try:
        assert (
            guard._audit_private_permissions(
                work_budget_seconds=4.0,
                wait_for_scan=False,
            )
            == "private permission audit is busy"
        )
        assert guard._permission_snapshot == (signature, checked_at, None)
        assert guard._permission_audit_failure is not None
        assert guard._permission_audit_failure[1] == "private permission audit is busy"
        assert guard.request_health()["ready"]

        guard._permission_snapshot = (signature, checked_at - 6.0, None)
        health = guard.request_health()
        assert not health["ready"]
        assert health["reason"] == "private permission audit is busy"
    finally:
        guard._permission_scan_lock.release()
        guard.close()


def test_permission_root_change_invalidates_the_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, signature = _ready_guard(tmp_path, monkeypatch)
    changed_signature = (*signature, 1)
    guard._permission_snapshot = (signature, time.monotonic(), None)
    signatures = iter((signature, changed_signature))
    monkeypatch.setattr(guard, "_permission_root_signature", lambda: next(signatures))
    monkeypatch.setattr(at_rest, "_private_permission_error", lambda *_args, **_kwargs: None)
    try:
        assert guard._audit_private_permissions(wait_for_scan=False) == (
            "private storage root changed during audit"
        )
        assert guard._permission_snapshot is None
        monkeypatch.setattr(guard, "_permission_root_signature", lambda: changed_signature)
        health = guard.request_health()
        assert not health["ready"]
        assert health["reason"] == "private storage root changed during audit"
    finally:
        guard.close()
