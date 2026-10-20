import importlib.util
import json
import os
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest


def _helper(*, root_markers: bool = False) -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "deployment-maintenance.py"
    spec = importlib.util.spec_from_file_location("deployment_maintenance", path)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if root_markers and os.name == "posix":
        original = module.read_marker

        def read_test_marker(path: Path) -> Any:
            try:
                actual = path.lstat()
            except OSError:
                return original(path)
            root_stat = os.stat_result(
                (
                    actual.st_mode & ~0o022,
                    actual.st_ino,
                    actual.st_dev,
                    actual.st_nlink,
                    0,
                    actual.st_gid,
                    actual.st_size,
                    actual.st_atime,
                    actual.st_mtime,
                    actual.st_ctime,
                )
            )
            with patch.object(Path, "lstat", return_value=root_stat):
                return original(path)

        module.read_marker = read_test_marker
        # Unit fixtures emulate privileged evidence ownership independently of
        # the production helper. Real root/service UID integration is separate.
        module._expected_evidence_owner = cast(Any, os).geteuid
    return module


def test_volume_preflight_groups_requirements_on_same_device(tmp_path: Path) -> None:
    helper = _helper()
    for name in ("releases", "backups", "data"):
        (tmp_path / name).mkdir()
    with (
        patch.object(helper.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1})()),
        pytest.raises(ValueError, match="headroom"),
    ):
        helper.preflight(tmp_path / "releases", tmp_path / "backups", tmp_path / "data")


def test_backup_policy_reports_unverified_attention_without_deleting_it(tmp_path: Path) -> None:
    helper = _helper(root_markers=True)
    backups = tmp_path / "backups"
    backups.mkdir()
    backup = backups / "noyra-unverified.noyra-backup"
    backup.write_bytes(b"backup")
    old = 1_700_000_000
    os.utime(backup, (old, old))

    plan = helper.backup_gc_plan(
        backups,
        helper.BackupRetentionPolicy(
            keep_verified_count=3,
            max_age_days=1,
            max_bytes=1,
            unverified_grace_days=1,
            minimum_recovery_points=1,
        ),
        now=helper.datetime.fromtimestamp(old + 3 * 86400, helper.UTC),
    )

    assert plan["backups"] == []
    assert plan["backup_attention"][0]["name"] == backup.name
    assert plan["backup_attention"][0]["reason"] == "unverified_backup_attention"


def test_backup_policy_plan_keeps_recovery_floor_and_records_sha_size_reason(
    tmp_path: Path,
) -> None:
    helper = _helper(root_markers=True)
    backups = tmp_path / "backups"
    backups.mkdir()
    now = helper.datetime(2026, 10, 17, tzinfo=helper.UTC)
    for index in range(4):
        backup = backups / f"noyra-verified-{index}.noyra-backup"
        backup.write_bytes(f"backup-{index}".encode())
        marker = {
            "format": "noyra-verified-upgrade-backup/v1",
            "backup": backup.name,
            "restore_verified": True,
            "sha256": helper.file_hash(backup),
            "verified_at": (now - timedelta(days=index)).isoformat(),
        }
        (backup.parent / f"{backup.name}.verified.json").write_text(json.dumps(marker))

    plan = helper.backup_gc_plan(
        backups,
        helper.BackupRetentionPolicy(
            keep_verified_count=3,
            max_age_days=1,
            max_bytes=1,
            unverified_grace_days=7,
            minimum_recovery_points=1,
        ),
        protected_names={"noyra-verified-3.noyra-backup"},
        now=now,
    )

    assert {row["name"] for row in plan["backup_details"]} == {
        "noyra-verified-2.noyra-backup",
        "noyra-verified-1.noyra-backup",
    }
    assert all(row["size"] > 0 and row["sha256"] for row in plan["backup_details"])
    assert {row["reason"] for row in plan["backup_details"]} == {
        "max_age_exceeded",
        "max_bytes_exceeded",
    }
    assert "noyra-verified-3.noyra-backup" in plan["backup_protected"]


def test_persisted_backup_policy_and_gc_run_history_are_read_by_root_helper(
    tmp_path: Path,
) -> None:
    from noyra.core import Database, IdentityStore
    from noyra.core.retention import RetentionManager, RetentionSettings
    from noyra.core.types import content_hash

    helper = _helper()
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-maintenance-policy"
    IdentityStore(database).ensure(subject, content_hash(subject))
    manager = RetentionManager(database)
    policy = helper.BackupRetentionPolicy(
        keep_verified_count=3,
        max_age_days=14,
        max_bytes=100,
        unverified_grace_days=5,
        minimum_recovery_points=2,
    ).public()
    manager.configure(
        subject,
        asdict(RetentionSettings()),
        expected_revision=0,
        actor="operator",
        backup_settings=policy,
        backup_expected_revision=0,
    )
    loaded_subject, loaded_policy = helper._database_subject_and_backup_policy(
        database.path,
        helper.BackupRetentionPolicy(),
    )
    assert loaded_subject == subject
    assert loaded_policy.public() == policy

    plan = {"plan_id": "a" * 64, "cursor": {"version": 1, "pending_names": []}}
    helper._record_backup_gc_run(
        database.path,
        subject,
        plan,
        status="planned",
    )
    with database.connection() as connection:
        row = connection.execute(
            "SELECT status, completed_at, cursor_json FROM backup_retention_runs"
        ).fetchone()
    assert row["status"] == "planned"
    assert row["completed_at"] is None
    assert json.loads(row["cursor_json"]) == plan["cursor"]


def test_backup_gc_required_audit_blocks_when_history_table_is_missing(tmp_path: Path) -> None:
    helper = _helper()
    database = tmp_path / "noyra.sqlite3"
    database.touch()
    plan = {"plan_id": "a" * 64, "cursor": {"version": 1}}

    with pytest.raises(RuntimeError, match="backup_retention_audit_unavailable"):
        helper._record_backup_gc_run(
            database,
            "Noyra-retention-audit",
            plan,
            status="planned",
            required=True,
        )


def test_backup_gc_required_audit_blocks_when_database_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _helper()
    database = tmp_path / "noyra.sqlite3"
    database.touch()
    plan = {"plan_id": "a" * 64, "cursor": {"version": 1}}

    def fail_connection(*_args: Any, **_kwargs: Any) -> Any:
        raise helper.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(helper.sqlite3, "connect", fail_connection)
    with pytest.raises(RuntimeError, match="backup_retention_audit_unavailable"):
        helper._record_backup_gc_run(
            database,
            "Noyra-retention-audit",
            plan,
            status="applied",
            required=True,
        )


def test_persisted_backup_policy_hash_failure_blocks_root_helper(tmp_path: Path) -> None:
    from noyra.core import Database, IdentityStore
    from noyra.core.retention import RetentionManager, RetentionSettings
    from noyra.core.types import content_hash

    helper = _helper()
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-maintenance-corruption"
    IdentityStore(database).ensure(subject, content_hash(subject))
    RetentionManager(database).configure(
        subject,
        asdict(RetentionSettings()),
        expected_revision=0,
        actor="operator",
        backup_settings=helper.BackupRetentionPolicy().public(),
        backup_expected_revision=0,
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE backup_retention_policies SET settings_json='{}' WHERE subject_id=?",
            (subject,),
        )
    with pytest.raises(ValueError, match="backup_retention_policy_unavailable"):
        helper._database_subject_and_backup_policy(
            database.path,
            helper.BackupRetentionPolicy(),
        )


def test_minimum_recovery_points_override_keep_count(tmp_path: Path) -> None:
    helper = _helper(root_markers=True)
    backups = tmp_path / "backups"
    backups.mkdir()
    now = helper.datetime(2026, 10, 17, tzinfo=helper.UTC)
    for index in range(3):
        backup = backups / f"noyra-floor-{index}.noyra-backup"
        backup.write_bytes(f"backup-{index}".encode())
        marker = {
            "format": "noyra-verified-upgrade-backup/v1",
            "backup": backup.name,
            "restore_verified": True,
            "sha256": helper.file_hash(backup),
            "verified_at": (now - timedelta(days=index)).isoformat(),
        }
        backup.with_name(backup.name + ".verified.json").write_text(json.dumps(marker))

    plan = helper.backup_gc_plan(
        backups,
        helper.BackupRetentionPolicy(
            keep_verified_count=1,
            max_age_days=None,
            max_bytes=None,
            minimum_recovery_points=2,
        ),
        now=now,
    )

    assert plan["backups"] == ["noyra-floor-2.noyra-backup"]
    assert plan["backup_summary"]["verified_recovery_points_after_plan"] == 2
    assert plan["backup_summary"]["recovery_points_status"] == "satisfied"


def test_backup_space_summary_exposes_unverified_pressure(tmp_path: Path) -> None:
    helper = _helper(root_markers=True)
    backups = tmp_path / "backups"
    backups.mkdir()
    now = helper.datetime(2026, 10, 17, tzinfo=helper.UTC)
    for index in range(2):
        backup = backups / f"noyra-verified-{index}.noyra-backup"
        backup.write_bytes(b"v" * 20)
        marker = {
            "format": "noyra-verified-upgrade-backup/v1",
            "backup": backup.name,
            "restore_verified": True,
            "sha256": helper.file_hash(backup),
            "verified_at": (now - timedelta(days=index)).isoformat(),
        }
        backup.with_name(backup.name + ".verified.json").write_text(json.dumps(marker))
    (backups / "noyra-unverified.noyra-backup").write_bytes(b"u" * 30)

    plan = helper.backup_gc_plan(
        backups,
        helper.BackupRetentionPolicy(
            keep_verified_count=2,
            max_age_days=None,
            max_bytes=1,
            minimum_recovery_points=2,
        ),
        now=now,
    )

    assert (
        plan["backup_summary"]["space_status"]
        == "unverified_and_protected_recovery_points_exceed_limit"
    )
    assert plan["backup_summary"]["total_bytes_after_plan"] == 70
    assert plan["backup_attention"] == []


def test_apply_gc_rechecks_backup_digest_after_preview(tmp_path: Path) -> None:
    helper = _helper(root_markers=True)
    releases, backups = tmp_path / "releases", tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    now = helper.datetime(2026, 10, 17, tzinfo=helper.UTC)
    original_by_name = {}
    for index in range(3):
        backup = backups / f"noyra-apply-{index}.noyra-backup"
        original = f"backup-{index}".encode()
        original_by_name[backup.name] = original
        backup.write_bytes(original)
        marker = {
            "format": "noyra-verified-upgrade-backup/v1",
            "backup": backup.name,
            "restore_verified": True,
            "sha256": helper.file_hash(backup),
            "verified_at": (now - timedelta(days=index)).isoformat(),
        }
        backup.with_name(backup.name + ".verified.json").write_text(json.dumps(marker))
    plan = {
        "releases": [],
        **helper.backup_gc_plan(
            backups,
            helper.BackupRetentionPolicy(
                keep_verified_count=2,
                max_age_days=None,
                max_bytes=None,
                minimum_recovery_points=1,
            ),
            now=now,
        ),
    }
    target = backups / plan["backups"][0]
    target.write_bytes(b"changed after preview")

    with pytest.raises(ValueError, match="unsafe_backup_path"):
        helper.apply_gc(releases, backups, plan)
    assert target.exists()
    target.write_bytes(original_by_name[target.name])
    helper.apply_gc(releases, backups, plan)
    assert not target.exists()
    assert not target.with_name(target.name + ".verified.json").exists()


def test_backup_gc_resumes_from_durable_cursor_after_process_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core import Database, IdentityStore
    from noyra.core.types import content_hash

    helper = _helper(root_markers=True)
    releases, backups = tmp_path / "releases", tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    release = releases / "old-release"
    release.mkdir()
    (release / helper.MARKER).write_text(
        json.dumps({"format": "noyra-verified-install/v1", "readiness": "passed"}),
        encoding="utf-8",
    )
    backup = backups / "noyra-resume.noyra-backup"
    backup.write_bytes(b"verified-backup")
    backup.with_name(backup.name + ".verified.json").write_text(
        json.dumps(
            {
                "format": "noyra-verified-upgrade-backup/v1",
                "backup": backup.name,
                "restore_verified": True,
                "sha256": helper.file_hash(backup),
            }
        ),
        encoding="utf-8",
    )
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-resumable-backup-gc"
    IdentityStore(database).ensure(subject, content_hash(subject))
    plan = {
        "plan_id": "a" * 64,
        "releases": [release.name],
        "backups": [backup.name],
        "backup_details": [
            {
                "name": backup.name,
                "size": backup.stat().st_size,
                "sha256": helper.file_hash(backup),
                "reason": "test",
            }
        ],
    }
    update = helper._update_backup_gc_run

    def interrupt_after_first_delete(
        database_path: Path,
        run_id: str,
        *,
        status: str,
        cursor: dict[str, Any],
        deleted_count: int,
        failure_code: str | None = None,
        required: bool = True,
    ) -> None:
        if status == "planned" and deleted_count == 1:
            raise KeyboardInterrupt
        update(
            database_path,
            run_id,
            status=status,
            cursor=cursor,
            deleted_count=deleted_count,
            failure_code=failure_code,
            required=required,
        )

    monkeypatch.setattr(helper, "_update_backup_gc_run", interrupt_after_first_delete)
    first_release = release
    with pytest.raises(KeyboardInterrupt):
        helper.apply_resumable_gc(database.path, subject, releases, backups, plan)
    assert not first_release.exists()

    monkeypatch.setattr(helper, "_update_backup_gc_run", update)
    result = helper.apply_resumable_gc(database.path, subject, releases, backups, plan)

    assert result["resumed"] is True
    assert result["deleted_count"] == 2
    assert not release.exists()
    assert not backup.exists()
    assert not backup.with_name(backup.name + ".verified.json").exists()
    with database.connection() as connection:
        row = connection.execute(
            "SELECT status, deleted_count, cursor_json FROM backup_retention_runs"
        ).fetchone()
    assert row["status"] == "applied"
    assert row["deleted_count"] == result["deleted_count"]
    assert json.loads(row["cursor_json"])["next_index"] == 2


def test_gc_manifest_cleanup_preserves_failed_and_pending_evidence(tmp_path: Path) -> None:
    from noyra.core import Database, IdentityStore
    from noyra.core.types import content_hash

    helper = _helper(root_markers=True)
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-gc-manifest-retention"
    IdentityStore(database).ensure(subject, content_hash(subject))
    statuses = ("planned", "failed", "applied")
    manifest_paths: dict[str, Path] = {}

    for status in statuses:
        run_id = f"backupretention_{status}"
        manifest = helper._write_gc_manifest(database.path, run_id, [])
        manifest_paths[status] = helper._gc_manifest_path(database.path, run_id)
        cursor = {
            "version": 4,
            "next_index": 0,
            "plan_id": "c" * 64,
            **manifest,
        }
        helper._record_backup_gc_run(
            database.path,
            subject,
            {"plan_id": "c" * 64},
            status=status,
            cursor_override=cursor,
            failure_code="RuntimeError" if status == "failed" else None,
            required=True,
            run_id=run_id,
        )

    orphan_path = helper._gc_manifest_path(database.path, "backupretention_orphan")
    helper._write_gc_manifest(database.path, "backupretention_orphan", [])

    helper._cleanup_orphan_gc_manifests(database.path)

    assert manifest_paths["planned"].exists()
    assert manifest_paths["failed"].exists()
    assert not manifest_paths["applied"].exists()
    assert not orphan_path.exists()
    with database.connection() as connection:
        row = connection.execute(
            "SELECT cursor_json, failure_code FROM backup_retention_runs WHERE run_id=?",
            ("backupretention_failed",),
        ).fetchone()
    cursor = json.loads(row["cursor_json"])
    failed_manifest = manifest_paths["failed"].read_bytes()
    assert row["failure_code"] == "RuntimeError"
    assert len(failed_manifest) == cursor["manifest_bytes"]
    assert helper.hashlib.sha256(failed_manifest).hexdigest() == cursor["manifest_sha256"]


def test_gc_runtime_failure_leaves_manifest_for_next_maintenance_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core import Database, IdentityStore
    from noyra.core.types import content_hash

    helper = _helper(root_markers=True)
    releases, backups = tmp_path / "releases", tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    release = releases / "old-release"
    release.mkdir()
    (release / helper.MARKER).write_text(
        json.dumps({"format": "noyra-verified-install/v1", "readiness": "passed"}),
        encoding="utf-8",
    )
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-gc-failed-manifest"
    IdentityStore(database).ensure(subject, content_hash(subject))
    plan = {
        "plan_id": "d" * 64,
        "releases": [release.name],
        "backup_details": [],
        "backups": [],
        "retention_policy": {},
        "backup_protected": [],
    }

    def fail_gc(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("injected GC action failure")

    monkeypatch.setattr(helper, "apply_gc", fail_gc)

    with pytest.raises(OSError):
        helper.apply_resumable_gc(database.path, subject, releases, backups, plan)

    with database.connection() as connection:
        row = connection.execute("SELECT run_id, status FROM backup_retention_runs").fetchone()
    assert row["status"] == "failed"
    manifest_path = helper._gc_index_path(database.path, row["run_id"])
    assert manifest_path.is_file()

    helper._cleanup_orphan_gc_manifests(database.path)

    assert manifest_path.is_file()
    assert release.is_dir()


def test_gc_manifest_cleanup_error_does_not_change_applied_run_to_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core import Database, IdentityStore
    from noyra.core.types import content_hash

    helper = _helper(root_markers=True)
    releases, backups = tmp_path / "releases", tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    release = releases / "old-release"
    release.mkdir()
    (release / helper.MARKER).write_text(
        json.dumps({"format": "noyra-verified-install/v1", "readiness": "passed"}),
        encoding="utf-8",
    )
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-gc-applied-manifest"
    IdentityStore(database).ensure(subject, content_hash(subject))
    plan = {
        "plan_id": "e" * 64,
        "releases": [release.name],
        "backup_details": [],
        "backups": [],
        "retention_policy": {},
        "backup_protected": [],
    }
    original_fsync = helper._fsync_directory
    fsync_calls = 0

    def fail_final_fsync(path: Path) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        with database.connection() as connection:
            applied = connection.execute(
                "SELECT 1 FROM backup_retention_runs WHERE status='applied'"
            ).fetchone()
        if applied is not None:
            raise OSError("injected sidecar directory sync failure")
        original_fsync(path)

    monkeypatch.setattr(helper, "_fsync_directory", fail_final_fsync)

    result = helper.apply_resumable_gc(database.path, subject, releases, backups, plan)

    with database.connection() as connection:
        row = connection.execute("SELECT status FROM backup_retention_runs").fetchone()
    assert result["status"] == "applied"
    assert result["manifest_cleanup_warning"] is True
    assert row["status"] == "applied"
    assert not release.exists()


def test_gc_resume_compatibility_does_not_delete_orphaned_marker(tmp_path: Path) -> None:
    helper = _helper(root_markers=True)
    releases, backups = tmp_path / "releases", tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    backup = backups / "noyra-orphan.noyra-backup"
    marker = backup.with_name(backup.name + ".verified.json")
    marker.write_text(
        json.dumps(
            {
                "format": "noyra-verified-upgrade-backup/v1",
                "backup": backup.name,
                "restore_verified": True,
                "sha256": "a" * 64,
                "size": 1,
            }
        ),
        encoding="utf-8",
    )
    action = {"kind": "backup", "name": backup.name, "sha256": "a" * 64, "size": 1}
    assert helper._resume_actions_compatible([action], [], releases, backups) is True
    assert marker.exists()


def test_gc_consumes_more_than_one_thousand_actions_in_batches(tmp_path: Path) -> None:
    from noyra.core import Database, IdentityStore
    from noyra.core.types import content_hash

    helper = _helper(root_markers=True)
    releases, backups = tmp_path / "releases", tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    names: list[str] = []
    for index in range(1_001):
        name = f"old-{index:04d}"
        release = releases / name
        release.mkdir()
        (release / helper.MARKER).write_text(
            json.dumps({"format": "noyra-verified-install/v1", "readiness": "passed"}),
            encoding="utf-8",
        )
        names.append(name)
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-gc-batch"
    IdentityStore(database).ensure(subject, content_hash(subject))
    plan = {
        "plan_id": "b" * 64,
        "releases": names,
        "backup_details": [],
        "backups": [],
        "retention_policy": {},
        "backup_protected": [],
    }
    first = helper.apply_resumable_gc(database.path, subject, releases, backups, plan)
    assert first["status"] == "partial"
    assert first["remaining"] == 1
    second = helper.apply_resumable_gc(database.path, subject, releases, backups, plan)
    assert second["status"] == "applied"
    assert second["remaining"] == 0
    assert not any(releases.iterdir())


def test_gc_ignores_unmanaged_releases_and_requires_verified_replacement(tmp_path: Path) -> None:
    helper = _helper(root_markers=True)
    releases, backups = tmp_path / "releases", tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    for index in range(8):
        release = releases / f"release-{index}"
        release.mkdir()
        (release / helper.MARKER).write_text(
            json.dumps({"format": "noyra-verified-install/v1", "readiness": "passed"})
        )
        backup = backups / f"noyra-release-{index}.noyra-backup"
        backup.write_bytes(b"backup")
        backup.with_name(backup.name + ".verified.json").write_text(
            json.dumps(
                {
                    "format": "noyra-verified-upgrade-backup/v1",
                    "backup": backup.name,
                    "restore_verified": True,
                    "sha256": helper.file_hash(backup),
                }
            )
        )
    unmanaged = releases / "unmanaged"
    unmanaged.mkdir()
    plan = helper.gc_plan(releases, backups, keep=3)
    assert "unmanaged" not in plan["releases"]
    assert len(plan["backups"]) == 5
    newest = max(
        backups.glob("*.noyra-backup"), key=lambda path: (path.stat().st_mtime_ns, path.name)
    )
    newest.write_bytes(b"tampered")
    assert helper.gc_plan(releases, backups, keep=3)["backups"] == []


def _releases(tmp_path: Path) -> tuple[Any, Path, Path]:
    helper = _helper(root_markers=True)
    releases, backups = tmp_path / "releases", tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    for index in range(8):
        release = releases / f"release-{index}"
        release.mkdir()
        (release / helper.MARKER).write_text(
            json.dumps({"format": "noyra-verified-install/v1", "readiness": "passed"})
        )
        # Filesystem timestamp precision must not decide which fixtures are old.
        os.utime(release, (index + 1, index + 1))
    return helper, releases, backups


def test_gc_protects_current_previous_and_process_references(tmp_path: Path) -> None:
    helper, releases, backups = _releases(tmp_path)
    try:
        (tmp_path / "current").symlink_to(releases / "release-0", target_is_directory=True)
        (tmp_path / "previous").symlink_to(releases / "release-1", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink permission unavailable")
    proc = tmp_path / "proc" / "123"
    proc.mkdir(parents=True)
    (proc / "maps").write_text(f"12345 {releases / 'release-2'}/library.so\n", encoding="utf-8")
    (proc / "cmdline").write_bytes(str(releases / "release-3" / "python").encode() + b"\0")
    plan = helper.gc_plan(releases, backups, keep=3, proc_root=proc.parent)
    assert plan["releases"] == ["release-4"]


@pytest.mark.parametrize("reference", ["maps", "cmdline"])
def test_gc_defers_release_deletion_when_process_reference_is_oversized(
    tmp_path: Path, reference: str
) -> None:
    helper, releases, backups = _releases(tmp_path)
    proc = tmp_path / "proc" / "123"
    proc.mkdir(parents=True)
    (proc / "maps").write_bytes(b"")
    (proc / "cmdline").write_bytes(b"")
    (proc / reference).write_bytes(b"x" * (helper.PROCESS_REFERENCE_BYTES + 1))
    assert helper.gc_plan(releases, backups, keep=3, proc_root=proc.parent)["releases"] == []


def test_gc_ignores_incomplete_markers(tmp_path: Path) -> None:
    helper, releases, backups = _releases(tmp_path)
    (releases / "release-0" / helper.MARKER).write_text(
        json.dumps({"format": "noyra-verified-install/v1", "readiness": "failed"})
    )
    for index, changes in enumerate(
        ({"format": "other"}, {"backup": "different"}, {"sha256": "not-a-digest"})
    ):
        backup = backups / f"noyra-invalid-{index}.noyra-backup"
        backup.write_bytes(b"backup")
        marker = {
            "format": "noyra-verified-upgrade-backup/v1",
            "backup": backup.name,
            "restore_verified": True,
            "sha256": helper.file_hash(backup),
            **changes,
        }
        backup.with_name(backup.name + ".verified.json").write_text(json.dumps(marker))
    plan = helper.gc_plan(releases, backups, keep=3, proc_root=tmp_path / "no-proc")
    assert "release-0" not in plan["releases"]
    assert plan["backups"] == []


def test_gc_refuses_pointer_outside_releases(tmp_path: Path) -> None:
    helper, releases, backups = _releases(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (tmp_path / "current").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink permission unavailable")
    with pytest.raises(ValueError, match="unsafe_release_pointer"):
        helper.gc_plan(releases, backups, proc_root=tmp_path / "no-proc")


@pytest.mark.skipif(os.name != "posix", reason="POSIX marker ownership contract")
def test_marker_rejects_unprivileged_owner_and_group_write(tmp_path: Path) -> None:
    helper = _helper()
    marker = tmp_path / "marker.json"
    marker.write_text(json.dumps({"format": "noyra-verified-install/v1"}))
    actual = marker.stat()
    for owner, mode in ((1001, 0o100644), (0, 0o100664)):
        metadata = os.stat_result(
            (
                mode,
                actual.st_ino,
                actual.st_dev,
                actual.st_nlink,
                owner,
                actual.st_gid,
                actual.st_size,
                actual.st_atime,
                actual.st_mtime,
                actual.st_ctime,
            )
        )
        with patch.object(Path, "lstat", return_value=metadata):
            assert helper.read_marker(marker) is None


def test_installer_preflights_before_stopping_and_gc_follows_readiness() -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts" / "install-ubuntu.sh").read_text()
    assert source.index('deployment-maintenance.py" preflight') < source.index(
        "\nstop_old_service\n", source.index('deployment-maintenance.py" preflight')
    )
    prepare = source.index('staging="$RELEASES_DIR/.staging-')
    stop = source.index("\nstop_old_service\n", prepare)
    verify = source.index('deployment-maintenance.py" verify-backup')
    assert prepare < stop < verify < source.index("\nensure_upgrade_state_layout\n")
    assert (
        '"$staging/.venv/bin/python" -I '
        '"$SOURCE_DIR/scripts/deployment-maintenance.py" verify-backup' in source
    )
    assert source.index("if start_and_check; then") < source.index('deployment-maintenance.py" gc')


def _real_gc_layout(tmp_path: Path, helper: Any) -> tuple[Path, Path]:
    releases, backups = tmp_path / "releases", tmp_path / "backups"
    releases.mkdir()
    backups.mkdir()
    for index in range(8):
        release = releases / f"release-{index}"
        release.mkdir()
        (release / helper.MARKER).write_text(
            json.dumps({"format": "noyra-verified-install/v1", "readiness": "passed"})
        )
        os.utime(release, (1_800_000_000 + index, 1_800_000_000 + index))
        backup = backups / f"noyra-{index}.noyra-backup"
        backup.write_bytes(f"backup-{index}".encode())
        backup.with_name(backup.name + ".verified.json").write_text(
            json.dumps(
                {
                    "format": "noyra-verified-upgrade-backup/v1",
                    "backup": backup.name,
                    "restore_verified": True,
                    "sha256": helper.file_hash(backup),
                    "size": backup.stat().st_size,
                }
            )
        )
        os.utime(backup, (1_800_000_000 + index, 1_800_000_000 + index))
    return releases, backups


def test_gc_rebuilds_plan_between_every_mixed_batch(tmp_path: Path) -> None:
    from noyra.core import Database, IdentityStore
    from noyra.core.types import content_hash

    helper = _helper(root_markers=True)
    helper.MAX_RESUMABLE_GC_ACTIONS = 1
    releases, backups = _real_gc_layout(tmp_path, helper)
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-mixed-resume"
    IdentityStore(database).ensure(subject, content_hash(subject))
    statuses = []
    for _ in range(12):
        plan = helper.gc_plan(releases, backups, keep=3, proc_root=tmp_path / "no-proc")
        result = helper.apply_resumable_gc(database.path, subject, releases, backups, plan)
        statuses.append(result["status"])
        if result["status"] == "applied":
            break
    assert statuses == ["partial"] * 9 + ["applied"]
    assert len(list(releases.iterdir())) == 3
    assert len(list(backups.glob("*.noyra-backup"))) == 3
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT status,deleted_count FROM backup_retention_runs"
        ).fetchall()
    assert [(row["status"], row["deleted_count"]) for row in rows] == [("applied", 10)]


def test_gc_policy_conflict_terminates_old_plan_and_preserves_evidence(tmp_path: Path) -> None:
    from noyra.core import Database, IdentityStore
    from noyra.core.types import content_hash

    helper = _helper(root_markers=True)
    helper.MAX_RESUMABLE_GC_ACTIONS = 1
    releases, backups = _real_gc_layout(tmp_path, helper)
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-supersede"
    IdentityStore(database).ensure(subject, content_hash(subject))
    first = helper.apply_resumable_gc(
        database.path,
        subject,
        releases,
        backups,
        helper.gc_plan(releases, backups, keep=3, proc_root=tmp_path / "no-proc"),
    )
    protected_releases = sorted(
        releases.iterdir(), key=lambda p: p.stat().st_mtime_ns, reverse=True
    )[:4]
    second = helper.apply_resumable_gc(
        database.path,
        subject,
        releases,
        backups,
        helper.gc_plan(releases, backups, keep=4, proc_root=tmp_path / "no-proc"),
    )
    assert second["run_id"] != first["run_id"]
    assert second["superseded_run_id"] == first["run_id"]
    assert helper._gc_index_path(database.path, first["run_id"]).exists()
    with database.connection() as connection:
        row = connection.execute(
            "SELECT status,deleted_count,failure_code FROM backup_retention_runs WHERE run_id=?",
            (first["run_id"],),
        ).fetchone()
    assert tuple(row) == ("failed", 1, "plan_superseded")
    assert all(release.is_dir() for release in protected_releases)
    for index in range(4, 8):
        assert (backups / f"noyra-{index}.noyra-backup").is_file()


def test_gc_index_tamper_fails_before_further_deletion(tmp_path: Path) -> None:
    import sqlite3

    from noyra.core import Database, IdentityStore
    from noyra.core.types import content_hash

    helper = _helper(root_markers=True)
    helper.MAX_RESUMABLE_GC_ACTIONS = 1
    releases, backups = _real_gc_layout(tmp_path, helper)
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-index-corruption"
    IdentityStore(database).ensure(subject, content_hash(subject))
    plan = helper.gc_plan(releases, backups, keep=3, proc_root=tmp_path / "no-proc")
    result = helper.apply_resumable_gc(database.path, subject, releases, backups, plan)
    with sqlite3.connect(helper._gc_index_path(database.path, result["run_id"])) as connection:
        connection.execute("UPDATE actions SET payload='{}' WHERE ordinal=1")
    before = sorted(path.name for path in releases.iterdir())
    with pytest.raises(ValueError, match="manifest_integrity"):
        helper.apply_resumable_gc(
            database.path,
            subject,
            releases,
            backups,
            helper.gc_plan(releases, backups, keep=3, proc_root=tmp_path / "no-proc"),
        )
    assert sorted(path.name for path in releases.iterdir()) == before


@pytest.mark.skipif(
    os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0,
    reason="requires actual root and separate service UID",
)
def test_root_gc_and_service_uid_health_restart_backup_restore() -> None:
    import shutil
    import subprocess
    import sys
    import tempfile

    from noyra.core import Database, IdentityStore
    from noyra.core.at_rest import BackupKeyring
    from noyra.core.types import content_hash

    outer = Path(tempfile.mkdtemp(prefix="noyra-gc-uid-"))
    outer.chmod(0o755)
    try:
        data = outer / "data"
        data.mkdir(mode=0o700)
        database = Database(data / "noyra.sqlite3")
        subject = "Noyra-real-UID"
        IdentityStore(database).ensure(subject, content_hash(subject))
        for member in data.iterdir():
            cast(Any, os).chown(member, 65534, 65534)
            member.chmod(0o600)
        cast(Any, os).chown(data, 65534, 65534)
        helper = _helper()
        helper.MAX_RESUMABLE_GC_ACTIONS = 1
        releases, backups = _real_gc_layout(outer, helper)
        result = helper.apply_resumable_gc(
            database.path,
            subject,
            releases,
            backups,
            helper.gc_plan(releases, backups, keep=3, proc_root=outer / "no-proc"),
        )
        assert result["status"] == "partial"
        evidence = helper._gc_index_path(database.path, result["run_id"])
        assert evidence.stat().st_uid == 0
        output = outer / "service-output"
        output.mkdir(mode=0o700)
        cast(Any, os).chown(output, 65534, 65534)
        keyring = outer / "keyring.json"
        BackupKeyring.initialize(keyring)
        cast(Any, os).chown(keyring, 65534, 65534)
        code = """
from pathlib import Path
import os, sys
from noyra.core.at_rest import (
    _harden_private_paths, _private_permission_error, EncryptedBackupManager,
)
root, output, keyring = map(Path, sys.argv[1:])
os.setgroups([]); os.setgid(65534); os.setuid(65534); os.umask(0o077)
assert _private_permission_error(root) is None, _private_permission_error(root)
_harden_private_paths(root)
assert _private_permission_error(root) is None, _private_permission_error(root)
manager = EncryptedBackupManager(root, keyring)
artifact = manager.create(output / 'offline.noyra-backup')
restored = manager.restore(artifact.path, output / 'restored')
assert not (restored / '.noyra-maintenance').exists()
assert _private_permission_error(root) is None, _private_permission_error(root)
assert _private_permission_error(restored) is None, _private_permission_error(restored)
"""
        completed = subprocess.run(
            [sys.executable, "-c", code, str(data), str(output), str(keyring)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert evidence.stat().st_uid == 0
        # A restored database keeps the old audit row but cannot replay this
        # machine's deletion queue. A fresh plan explicitly terminates it.
        restored_db = output / "restored" / "noyra.sqlite3"
        restored_result = helper.apply_resumable_gc(
            restored_db,
            subject,
            releases,
            backups,
            helper.gc_plan(releases, backups, keep=4, proc_root=outer / "no-proc"),
        )
        assert restored_result["superseded_run_id"] == result["run_id"]
    finally:
        shutil.rmtree(outer)


@pytest.mark.skipif(
    os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0,
    reason="real privileged evidence metadata",
)
def test_gc_legacy_and_reserved_namespace_keep_strict_metadata(tmp_path: Path) -> None:
    from noyra.core.at_rest import AtRestError, _private_paths

    helper = _helper()
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    database = data / "noyra.sqlite3"
    database.touch(mode=0o600)
    legacy = database.with_name(".noyra.sqlite3.backup-retention-backupretention_legacy.manifest")
    legacy.write_text("legacy-evidence", encoding="utf-8")
    legacy.chmod(0o600)
    assert legacy not in list(_private_paths(data))
    helper._ensure_gc_evidence(database)
    moved = helper._gc_manifest_path(database, "backupretention_legacy")
    assert moved.read_text(encoding="utf-8") == "legacy-evidence"
    assert not legacy.exists()
    assert moved not in list(_private_paths(data))
    moved.chmod(0o644)
    with pytest.raises(AtRestError, match="maintenance evidence"):
        list(_private_paths(data))
    moved.chmod(0o600)
    alias = tmp_path / "alias"
    alias.hardlink_to(moved)
    with pytest.raises(AtRestError, match="maintenance evidence"):
        list(_private_paths(data))


@pytest.mark.parametrize("version", [2, 3, 4])
def test_gc_upgrades_legacy_cursor_after_a_completed_mixed_action(
    tmp_path: Path, version: int
) -> None:
    import shutil

    from noyra.core import Database, IdentityStore
    from noyra.core.types import content_hash

    helper = _helper(root_markers=True)
    helper.MAX_RESUMABLE_GC_ACTIONS = 2
    releases, backups = _real_gc_layout(tmp_path, helper)
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-legacy-gc"
    IdentityStore(database).ensure(subject, content_hash(subject))
    plan = helper.gc_plan(releases, backups, keep=3, proc_root=tmp_path / "no-proc")
    actions = list(helper._iter_gc_actions(releases, plan))
    assert actions[0]["kind"] == "release" and any(a["kind"] == "backup" for a in actions)
    run_id = f"backupretention_legacy_{version}"
    cursor = {
        "version": version,
        "next_index": 1,
        "plan_id": plan["plan_id"],
        "policy_digest": helper._plan_policy_digest(plan),
        "protected_digest": helper._plan_protected_digest(plan),
    }
    if version == 4:
        cursor.update(helper._write_gc_manifest(database.path, run_id, actions))
    else:
        cursor["actions"] = actions
    helper._record_backup_gc_run(
        database.path,
        subject,
        plan,
        status="planned",
        deleted_count=1,
        required=True,
        cursor_override=cursor,
        run_id=run_id,
    )
    shutil.rmtree(releases / actions[0]["name"])
    for _ in range(6):
        rebuilt = helper.gc_plan(releases, backups, keep=3, proc_root=tmp_path / "no-proc")
        result = helper.apply_resumable_gc(database.path, subject, releases, backups, rebuilt)
        assert result["run_id"] == run_id and result["resumed"] is True
        if result["status"] == "applied":
            break
    assert result["status"] == "applied" and result["deleted_count"] == len(actions)
    with database.connection() as connection:
        row = connection.execute("SELECT * FROM backup_retention_runs").fetchone()
    assert row["status"] == "applied" and json.loads(row["cursor_json"])["version"] == 5
    assert len(list(releases.iterdir())) == 3
    assert len(list(backups.glob("*.noyra-backup"))) == 3


def test_gc_index_resume_supports_relative_database_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core import Database, IdentityStore
    from noyra.core.types import content_hash

    helper, releases, backups = _releases(tmp_path)
    helper.MAX_RESUMABLE_GC_ACTIONS = 1
    database = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-relative-gc"
    IdentityStore(database).ensure(subject, content_hash(subject))
    plan = helper.gc_plan(releases, backups, keep=3, proc_root=tmp_path / "no-proc")
    monkeypatch.chdir(tmp_path)
    for _ in range(5):
        result = helper.apply_resumable_gc(
            Path(database.path.name), subject, releases, backups, plan
        )
    assert result["status"] == "applied" and result["deleted_count"] == 5
    assert len(list(releases.iterdir())) == 3
