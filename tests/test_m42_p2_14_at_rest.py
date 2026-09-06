from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import ValidationError

from noyra.core.at_rest import (
    VOLUME_ATTESTATION_FORMAT,
    AtRestConfig,
    AtRestError,
    AtRestGuard,
    BackupAuthenticationError,
    BackupKeyring,
    BackupKeyUnavailableError,
    EncryptedBackupManager,
    VolumeEncryptionProbe,
    VolumeEncryptionStatus,
    _attestation_permission_error,
    _harden_private_paths,
    _is_posix_lost_found,
    _posix_metadata_error,
    _private_paths,
    _private_permission_error,
    _windows_permission_audit,
    validate_keyring_path,
    validate_private_file,
    validate_private_root,
)
from noyra.core.database import Database
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.identity import IdentityStore
from noyra.core.locking import ProcessLock
from noyra.core.types import content_hash
from noyra.service import NoyraService, ServiceSettings

ROOT = Path(__file__).resolve().parents[1]


def _posix_root_available() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return os.name == "posix" and callable(geteuid) and int(geteuid()) == 0


def _chown(path: Path, uid: int, gid: int) -> None:
    chown = getattr(os, "chown", None)
    if not callable(chown):
        pytest.skip("POSIX ownership controls are unavailable")
    chown(path, uid, gid)


@pytest.fixture(autouse=True)
def _restore_process_umask() -> Iterator[None]:
    if os.name == "nt":
        yield
        return
    previous = os.umask(0)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


class _EncryptedProbe:
    def probe(
        self,
        data_root: Path | str,
        *,
        backend: str,
        attestation_path: Path | None,
    ) -> VolumeEncryptionStatus:
        del data_root, backend, attestation_path
        return VolumeEncryptionStatus(True, "test-encrypted", "verified test volume", "volume-1")


def _runtime(data_root: Path) -> None:
    database = Database(data_root / "noyra.sqlite3")
    IdentityStore(database).ensure(
        "Noyra-p214-backup",
        content_hash({"seed": "p2-14"}),
    )
    secret_dir = data_root / "secrets" / "models"
    secret_dir.mkdir(parents=True)
    (secret_dir / "provider.key").write_text("P2-14-PRIVATE-CREDENTIAL", encoding="utf-8")
    subject_dir = data_root / "subject" / "cold"
    subject_dir.mkdir(parents=True)
    (subject_dir / "segment.bin").write_bytes(b"private-archive-segment")


def _keyring(tmp_path: Path) -> Path:
    path = tmp_path / "offline" / "backup-keyring.json"
    BackupKeyring.initialize(path)
    return path


def test_platform_private_permissions_are_hardened_and_audited(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    secret_dir = data_root / "secrets"
    secret_dir.mkdir(parents=True)
    (secret_dir / "credential.json").write_text("secret", encoding="utf-8")
    private_files = []
    for name in ("subject", "training_raw", "workspace", "cache", "exports"):
        private_dir = data_root / name / "nested"
        private_dir.mkdir(parents=True)
        private_file = private_dir / "private.bin"
        private_file.write_bytes(name.encode("utf-8"))
        private_files.append(private_file)
    if os.name != "nt":
        os.chmod(data_root, 0o755)
        os.chmod(secret_dir / "credential.json", 0o644)
        for path in private_files:
            os.chmod(path.parent, 0o755)
            os.chmod(path, 0o644)
        assert _private_permission_error(data_root) is not None

    _harden_private_paths(data_root)

    assert _private_permission_error(data_root) is None
    if os.name != "nt":
        assert data_root.stat().st_mode & 0o777 == 0o700
        assert (secret_dir / "credential.json").stat().st_mode & 0o777 == 0o600
        assert all(path.parent.stat().st_mode & 0o777 == 0o700 for path in private_files)
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in private_files)


def test_posix_lost_found_is_reserved_only_with_standard_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX filesystem contract")
    if not _posix_root_available():
        pytest.skip("requires root to model ext4 lost+found ownership")

    data_root = tmp_path / "data"
    data_root.mkdir()
    lost_found = data_root / "lost+found"
    lost_found.mkdir()
    os.chmod(lost_found, 0o700)
    _chown(lost_found, 0, 0)
    _chown(data_root, 1000, 1000)
    os.chmod(data_root, 0o700)
    monkeypatch.setattr("noyra.core.at_rest._effective_uid", lambda: 1000)

    assert _is_posix_lost_found(data_root, lost_found)
    assert lost_found not in set(_private_paths(data_root))
    assert _private_permission_error(data_root) is None

    chmod_calls: list[Path] = []
    chmod = os.chmod

    def record_chmod(path: str | bytes | Path, mode: int, *, dir_fd: int | None = None) -> None:
        chmod_calls.append(Path(os.fsdecode(path)))
        if dir_fd is None:
            chmod(path, mode)
        else:
            chmod(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr("noyra.core.at_rest.os.chmod", record_chmod)
    _harden_private_paths(data_root)
    assert lost_found not in chmod_calls

    os.chmod(lost_found, 0o755)
    assert not _is_posix_lost_found(data_root, lost_found)
    with pytest.raises(AtRestError, match=r"lost\+found.*mode 0700"):
        list(_private_paths(data_root))

    os.chmod(lost_found, 0o700)
    _chown(lost_found, 1000, 1000)
    with pytest.raises(AtRestError, match=r"lost\+found.*root-owned"):
        list(_private_paths(data_root))


def test_posix_root_owned_non_reserved_entry_remains_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX filesystem contract")
    if not _posix_root_available():
        pytest.skip("requires root to model a service account")

    data_root = tmp_path / "data"
    data_root.mkdir()
    root_owned = data_root / "root-owned"
    root_owned.mkdir()
    _chown(data_root, 1000, 1000)
    _chown(root_owned, 0, 0)
    os.chmod(data_root, 0o700)
    os.chmod(root_owned, 0o700)
    monkeypatch.setattr("noyra.core.at_rest._effective_uid", lambda: 1000)

    assert _private_permission_error(data_root) == (
        "private storage is not owned by the service account: root-owned"
    )
    assert root_owned in set(_private_paths(data_root))


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_posix_lost_found_invalid_shape_fails_closed(tmp_path: Path, kind: str) -> None:
    if os.name != "posix":
        pytest.skip("POSIX filesystem contract")
    if not _posix_root_available():
        pytest.skip("requires root to model ext4 lost+found ownership")

    data_root = tmp_path / "data"
    data_root.mkdir()
    lost_found = data_root / "lost+found"
    if kind == "file":
        lost_found.write_text("not a directory", encoding="utf-8")
    else:
        target = tmp_path / "outside"
        target.mkdir()
        try:
            lost_found.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("filesystem symlinks are unavailable")

    with pytest.raises(AtRestError, match=r"lost\+found"):
        list(_private_paths(data_root))


def test_backup_omits_standard_posix_lost_found(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX filesystem contract")
    if not _posix_root_available():
        pytest.skip("requires root to model ext4 lost+found ownership")

    data_root = tmp_path / "data"
    data_root.mkdir()
    _runtime(data_root)
    lost_found = data_root / "lost+found"
    lost_found.mkdir()
    (lost_found / "recovered").write_text("filesystem metadata", encoding="utf-8")
    _chown(lost_found, 0, 0)
    _chown(lost_found / "recovered", 0, 0)
    os.chmod(lost_found, 0o700)
    os.chmod(lost_found / "recovered", 0o600)

    manager = EncryptedBackupManager(
        data_root,
        _keyring(tmp_path),
        max_total_bytes=100_000_000,
    )
    backup = tmp_path / "lost-found.noyra-backup"
    manager.create(backup)
    restored = manager.restore(backup, tmp_path / "restored")

    assert not (restored / "lost+found").exists()


def test_private_root_and_file_reject_wrong_device_boundary(tmp_path: Path) -> None:
    root = validate_private_root(tmp_path / "private", create=True)
    wrong_device = root.stat().st_dev + 1
    with pytest.raises(AtRestError, match="device boundary"):
        validate_private_root(root, expected_device=wrong_device)
    private_file = root / "secret.key"
    private_file.write_text("secret", encoding="utf-8")
    with pytest.raises(AtRestError, match="device boundary"):
        validate_private_file(root, private_file.name, expected_device=wrong_device)


def test_private_tree_rejects_nested_symlink_or_reparse_point(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    workspace = data_root / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir(parents=True)
    outside.mkdir()
    _harden_private_paths(data_root)
    try:
        (workspace / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem symlinks are unavailable")
    try:
        error = _private_permission_error(data_root)
    except AtRestError as caught:
        assert "symlink" in str(caught) or "reparse" in str(caught)
    else:
        assert error is not None
        assert "symlink" in error or "reparse" in error


def test_windows_attestation_requires_trusted_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = tmp_path / "attestation.json"
    attestation.write_text("{}", encoding="utf-8")
    owner_requirements: list[tuple[bool, bool]] = []

    def audit(
        path: Path,
        *,
        runner: object,
        require_current_owner: bool,
        require_privileged_owner: bool,
    ) -> tuple[bool, str]:
        del path, runner
        owner_requirements.append((require_current_owner, require_privileged_owner))
        return True, "private ACL"

    monkeypatch.setattr("noyra.core.at_rest._windows_permission_audit", audit)

    def runner(_: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "", "")

    assert _attestation_permission_error(attestation, "nt", runner) is None
    assert owner_requirements == [(True, True)]


def test_deployment_and_threat_model_require_the_complete_at_rest_boundary() -> None:
    deploy_env = (ROOT / "deploy" / "noyra.env.example").read_text(encoding="utf-8")
    unit = (ROOT / "deploy" / "systemd" / "noyra.service").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install-ubuntu.sh").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    threat_model = (ROOT / "docs" / "security" / "threat-model.md").read_text(encoding="utf-8")
    openapi = (ROOT / "docs" / "api" / "openapi.yaml").read_text(encoding="utf-8")

    assert "NOYRA_AT_REST_MODE=required" in deploy_env
    assert "NOYRA_BACKUP_KEYRING_PATH=/etc/noyra/backup-keyring.json" in deploy_env
    assert "UMask=0077" in unit
    assert "ReadOnlyPaths=/etc/noyra/backup-keyring.json" in unit
    assert "backup-key init" in installer
    assert "chmod 0640" in installer
    assert "NOYRA_VOLUME_ENCRYPTION_BACKEND: attestation" in compose
    assert "volume-attestation.json:ro" in compose
    assert "live host root" in threat_model
    assert "Windows Administrator" in threat_model
    assert "chunked AES-256-GCM" in threat_model
    assert "there is no plaintext fallback" in threat_model
    assert "AtRestHealth:" in openapi
    assert "at_rest:" in openapi


def test_windows_acl_audit_rejects_other_principals(tmp_path: Path) -> None:
    root = tmp_path / "acl"
    root.mkdir()

    def unsafe_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        del args
        return subprocess.CompletedProcess([], 0, '{"Ready":false,"Detail":"other principal"}', "")

    def safe_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        del args
        return subprocess.CompletedProcess([], 0, '{"Ready":true,"Detail":"private ACL"}', "")

    assert _windows_permission_audit(root, runner=unsafe_runner) == (False, "other principal")
    assert _windows_permission_audit(root, runner=safe_runner) == (True, "private ACL")


def test_posix_permission_contract_rejects_group_or_other_access() -> None:
    assert (
        _posix_metadata_error(
            [("data", True, 1000, 0o755), ("secret", False, 1000, 0o600)],
            1000,
        )
        == "private directory is accessible by group/other: data"
    )
    assert (
        _posix_metadata_error(
            [("data", True, 1000, 0o700), ("secret", False, 1000, 0o640)],
            1000,
        )
        == "private file is accessible by group/other: secret"
    )
    assert (
        _posix_metadata_error(
            [("data", True, 1000, 0o700), ("secret", False, 1000, 0o600)],
            1000,
        )
        is None
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "MountPoint": "C:\\",
                "VolumeStatus": "FullyEncrypted",
                "ProtectionStatus": "On",
                "EncryptionPercentage": 100,
            },
            True,
        ),
        (
            {
                "MountPoint": "C:\\",
                "VolumeStatus": "EncryptionInProgress",
                "ProtectionStatus": "Off",
                "EncryptionPercentage": 50,
            },
            False,
        ),
    ],
)
def test_bitlocker_probe_requires_full_encryption_and_protection(
    tmp_path: Path,
    payload: dict[str, object],
    expected: bool,
) -> None:
    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        del args
        return subprocess.CompletedProcess([], 0, json.dumps(payload), "")

    status = VolumeEncryptionProbe(platform_name="nt", runner=runner).probe(
        tmp_path,
        backend="auto",
        attestation_path=None,
    )
    assert status.encrypted is expected
    assert status.backend == "bitlocker"


def test_luks_probe_follows_device_mapper_ancestors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sysfs = tmp_path / "sys"
    mapped = sysfs / "devices" / "virtual" / "block" / "dm-0"
    (mapped / "dm").mkdir(parents=True)
    (mapped / "dm" / "uuid").write_text("CRYPT-LUKS2-test-volume", encoding="utf-8")
    device = sysfs / "dev" / "block"
    device.mkdir(parents=True)
    try:
        (device / "8:1").symlink_to(mapped, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem symlinks are unavailable")
    monkeypatch.setattr("noyra.core.at_rest._device_major", lambda value: 8)
    monkeypatch.setattr("noyra.core.at_rest._device_minor", lambda value: 1)

    status = VolumeEncryptionProbe(platform_name="posix", sysfs_root=sysfs).probe(
        tmp_path,
        backend="auto",
        attestation_path=None,
    )

    assert status.encrypted is True
    assert status.backend == "luks"


def test_container_attestation_is_scoped_and_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = (tmp_path / "data").resolve()
    data_root.mkdir()
    attestation = tmp_path / "volume-attestation.json"
    payload = {
        "format": VOLUME_ATTESTATION_FORMAT,
        "data_root": str(data_root),
        "encrypted": True,
        "provider": "host-luks",
        "volume_id": "luks-volume-1",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("noyra.core.at_rest._attestation_permission_error", lambda *args: None)
    probe = VolumeEncryptionProbe(platform_name="posix")

    assert probe.probe(data_root, backend="attestation", attestation_path=attestation).encrypted
    payload["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    assert not probe.probe(
        data_root,
        backend="attestation",
        attestation_path=attestation,
    ).encrypted
    payload["expires_at"] = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    overlong = probe.probe(
        data_root,
        backend="attestation",
        attestation_path=attestation,
    )
    assert not overlong.encrypted
    assert "24 hours" in overlong.detail


def test_required_guard_fails_closed_and_health_tracks_key_loss(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    keyring_path = _keyring(tmp_path)
    guard = AtRestGuard(
        AtRestConfig(
            data_root=data_root,
            mode="required",
            backup_keyring_path=keyring_path,
        ),
        probe=_EncryptedProbe(),  # type: ignore[arg-type]
    )
    guard.prepare()
    _runtime(data_root)
    guard.post_initialize()

    healthy = guard.require_ready()
    assert healthy["ready"] is True
    assert healthy["volume"]["encrypted"] is True
    assert "key_b64" not in json.dumps(healthy)

    keyring_path.unlink()
    degraded = guard.health()
    assert degraded["ready"] is False
    assert degraded["enforced"] is True
    with pytest.raises(BackupKeyUnavailableError):
        guard.require_ready()


def test_keyring_rejects_hard_links(tmp_path: Path) -> None:
    keyring_path = _keyring(tmp_path)
    linked_path = tmp_path / "offline" / "keyring-alias.json"
    linked_path.hardlink_to(keyring_path)
    with pytest.raises(BackupKeyUnavailableError, match="hard-linked"):
        validate_keyring_path(keyring_path)


def test_required_guard_rejects_unencrypted_volume_before_database_creation(tmp_path: Path) -> None:
    class UnencryptedProbe:
        def probe(self, *args: object, **kwargs: object) -> VolumeEncryptionStatus:
            del args, kwargs
            return VolumeEncryptionStatus(False, "test", "not encrypted")

    data_root = tmp_path / "data"
    guard = AtRestGuard(
        AtRestConfig(
            data_root=data_root,
            mode="required",
            backup_keyring_path=_keyring(tmp_path),
        ),
        probe=UnencryptedProbe(),  # type: ignore[arg-type]
    )

    with pytest.raises(AtRestError, match="encrypted volume requirement failed"):
        guard.prepare()
    assert not (data_root / "noyra.sqlite3").exists()


def test_service_settings_require_keyring_and_attestation_paths(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="NOYRA_BACKUP_KEYRING_PATH"):
        ServiceSettings(
            data_dir=tmp_path / "settings",
            subject_id="Noyra-p214-settings",
            genesis_hash=content_hash({"seed": "p2-14-settings"}),
            at_rest_mode="required",
        )
    with pytest.raises(ValidationError, match="NOYRA_VOLUME_ATTESTATION_PATH"):
        ServiceSettings(
            data_dir=tmp_path / "settings",
            subject_id="Noyra-p214-settings",
            genesis_hash=content_hash({"seed": "p2-14-settings"}),
            at_rest_mode="required",
            backup_keyring_path=tmp_path / "keyring.json",
            volume_encryption_backend="attestation",
        )


def test_backup_key_rotation_retains_old_restore_keys(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _runtime(data_root)
    keyring_path = _keyring(tmp_path)
    manager = EncryptedBackupManager(data_root, keyring_path, max_total_bytes=100_000_000)
    first = tmp_path / "first.noyra-backup"
    second = tmp_path / "second.noyra-backup"

    first_artifact = manager.create(first)
    rotated = BackupKeyring.rotate(keyring_path)
    second_artifact = manager.create(second)

    assert rotated.generation == 2
    assert first_artifact.key_id != second_artifact.key_id
    assert manager.restore(first, tmp_path / "restore-first").is_dir()
    assert manager.restore(second, tmp_path / "restore-second").is_dir()


def test_encrypted_backup_round_trip_hides_secrets_and_cleans_plaintext_work(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _runtime(data_root)
    keyring_path = _keyring(tmp_path)
    manager = EncryptedBackupManager(data_root, keyring_path, max_total_bytes=100_000_000)
    backup = tmp_path / "subject.noyra-backup"

    artifact = manager.create(backup)

    assert artifact.byte_size == backup.stat().st_size
    assert b"P2-14-PRIVATE-CREDENTIAL" not in backup.read_bytes()
    assert not list(data_root.glob(".noyra-backup-*"))
    restored = manager.restore(backup, tmp_path / "restored")
    assert (restored / "secrets" / "models" / "provider.key").read_text(
        encoding="utf-8"
    ) == "P2-14-PRIVATE-CREDENTIAL"
    assert (restored / "subject" / "cold" / "segment.bin").read_bytes() == (
        b"private-archive-segment"
    )
    assert not list(tmp_path.glob(".noyra-restore-*"))


def test_missing_historical_key_fails_without_partial_restore(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _runtime(data_root)
    keyring_path = _keyring(tmp_path)
    manager = EncryptedBackupManager(data_root, keyring_path, max_total_bytes=100_000_000)
    backup = tmp_path / "old.noyra-backup"
    manager.create(backup)
    rotated = BackupKeyring.rotate(keyring_path)
    active = rotated.active
    BackupKeyring(
        generation=rotated.generation,
        active_key_id=active.key_id,
        keys=(active,),
    ).write(keyring_path, create_only=False)
    target = tmp_path / "restore-missing-key"

    with pytest.raises(BackupKeyUnavailableError):
        manager.restore(backup, target)

    assert not target.exists()
    assert not list(tmp_path.glob(".noyra-restore-*"))


@pytest.mark.parametrize("mutation", ["tamper", "truncate"])
def test_tampered_or_truncated_backup_fails_without_publication(
    tmp_path: Path,
    mutation: str,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _runtime(data_root)
    keyring_path = _keyring(tmp_path)
    manager = EncryptedBackupManager(data_root, keyring_path, max_total_bytes=100_000_000)
    valid = tmp_path / "valid.noyra-backup"
    broken = tmp_path / "broken.noyra-backup"
    manager.create(valid)
    payload = bytearray(valid.read_bytes())
    if mutation == "tamper":
        payload[-20] ^= 0x01
    else:
        del payload[-20:]
    broken.write_bytes(payload)
    target = tmp_path / "restore-broken"

    with pytest.raises(BackupAuthenticationError):
        manager.restore(broken, target)

    assert not target.exists()


def test_backup_refuses_to_run_while_service_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _runtime(data_root)
    keyring_path = _keyring(tmp_path)
    lock = ProcessLock(data_root / "noyra.sqlite3.lock")
    lock.acquire()
    try:
        # A real msvcrt lock collision is reported as PermissionError(errno=13)
        # on supported Windows builds, without the explicit sharing-violation
        # winerror.  Keep the production classifier strict and make this
        # higher-level backup test deterministic by injecting its canonical
        # ownership signal after the first lock is held.
        def fail_acquire(_lock: ProcessLock) -> bool:
            raise RuntimeOwnershipError("injected lock contention")

        monkeypatch.setattr(ProcessLock, "acquire", fail_acquire)
        with pytest.raises(RuntimeOwnershipError, match="stop Noyra"):
            EncryptedBackupManager(
                data_root,
                keyring_path,
                max_total_bytes=100_000_000,
            ).create(tmp_path / "locked.noyra-backup")
    finally:
        lock.release()


def test_backup_rejects_symlinked_private_content(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _runtime(data_root)
    outside = tmp_path / "outside-secret"
    outside.write_text("outside", encoding="utf-8")
    link = data_root / "secrets" / "linked"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("filesystem symlinks are unavailable")

    with pytest.raises(AtRestError, match="symlink"):
        EncryptedBackupManager(
            data_root,
            _keyring(tmp_path),
            max_total_bytes=100_000_000,
        ).create(tmp_path / "linked.noyra-backup")


def test_service_health_exposes_non_secret_at_rest_status(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_dir=tmp_path / "service",
        subject_id="Noyra-p214-service-health",
        genesis_hash=content_hash({"seed": "p2-14-service"}),
        host="127.0.0.1",
        port=0,
    )
    service = NoyraService(settings)
    try:
        service.boot()
        service.http.start()
        _, port = service.http.address
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["at_rest"]["mode"] == "development"
        assert payload["at_rest"]["enforced"] is False
        assert "key_b64" not in json.dumps(payload)
    finally:
        service.http.close()
        service.kernel.close()


def test_service_api_fails_closed_when_required_boundary_degrades(tmp_path: Path) -> None:
    class FailedGuard:
        required = True

        @staticmethod
        def require_ready() -> dict[str, object]:
            raise AtRestError("test boundary unavailable")

    settings = ServiceSettings(
        data_dir=tmp_path / "service-fail-closed",
        subject_id="Noyra-p214-service-fail-closed",
        genesis_hash=content_hash({"seed": "p2-14-service-fail-closed"}),
        host="127.0.0.1",
        port=0,
    )
    service = NoyraService(settings)
    try:
        service.boot()
        service.http.at_rest = FailedGuard()
        service.http.start()
        _, port = service.http.address
        with pytest.raises(HTTPError) as get_error:
            urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5)
        assert get_error.value.code == 503
        request = Request(
            f"http://127.0.0.1:{port}/api/config/search-providers",
            data=b"{}",
            method="POST",
        )
        with pytest.raises(HTTPError) as post_error:
            urlopen(request, timeout=5)
        assert post_error.value.code == 503
    finally:
        service.http.close()
        service.kernel.close()


def test_backup_copy_does_not_leave_destination_on_failure(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _runtime(data_root)
    keyring_path = _keyring(tmp_path)
    manager = EncryptedBackupManager(data_root, keyring_path, max_total_bytes=1_000_000)
    destination = tmp_path / "oversized.noyra-backup"

    with pytest.raises(AtRestError, match="limit"):
        manager.create(destination)

    assert not destination.exists()
    assert not list(data_root.glob(".noyra-backup-*"))


def test_restore_preserves_preexisting_empty_target_on_failure(tmp_path: Path) -> None:
    keyring_path = _keyring(tmp_path)
    source = tmp_path / "invalid.noyra-backup"
    source.write_bytes(b"invalid")
    target = tmp_path / "empty-target"
    target.mkdir()

    with pytest.raises(BackupAuthenticationError):
        EncryptedBackupManager(tmp_path / "unused", keyring_path).restore(source, target)

    assert target.is_dir()
    assert not any(target.iterdir())


def test_backup_artifacts_can_be_copied_without_changing_restore_result(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _runtime(data_root)
    keyring_path = _keyring(tmp_path)
    manager = EncryptedBackupManager(data_root, keyring_path, max_total_bytes=100_000_000)
    original = tmp_path / "original.noyra-backup"
    copied = tmp_path / "copied.noyra-backup"
    manager.create(original)
    shutil.copyfile(original, copied)

    restored = manager.restore(copied, tmp_path / "copied-restore")

    assert (restored / "secrets" / "models" / "provider.key").is_file()
