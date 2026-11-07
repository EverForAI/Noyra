from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from noyra.core.at_rest import BackupKeyring, BackupKeyUnavailableError, _keyring_permission_error
from noyra.core.backup_keyring_paths import BackupRotationBoundary, BackupRotationLocation
from noyra.core.backup_rotation import BackupRotationJournal, RotationJournalError
from noyra.core.backup_rotation_records import (
    RotationAcceptance,
    RotationHeader,
    RotationIntent,
    RotationPreparationUnknownError,
    sha256,
)
from noyra.core.backup_rotation_storage import RotationStorage
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.locking import ProcessLock

POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX durable preparation")
REQ = "a" * 32


def setup(root: Path) -> tuple[Path, bytes, BackupRotationJournal]:
    target = root / "keyring.json"
    BackupKeyring.initialize(target)
    return target, target.read_bytes(), BackupRotationJournal(target)


def prepared(root: Path) -> tuple[Path, bytes, BackupRotationJournal]:
    target, source, api = setup(root)
    api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    return target, source, api


def directory(target: Path) -> Path:
    return target.with_name("." + target.name + ".rotation-journal")


def safe_write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def test_invalid_request_is_rejected_without_touching_storage(tmp_path: Path) -> None:
    import pytest

    from noyra.core.backup_rotation import BackupRotationJournal, RotationJournalError

    missing = tmp_path / "missing" / "keyring.json"
    with pytest.raises(RotationJournalError, match="request"):
        BackupRotationJournal(missing).prepare(
            request_id="../escape", expected_generation=1, expected_source_hash="a" * 64
        )
    assert not missing.parent.exists()


@pytest.mark.parametrize("generation,content", [(True, "a" * 64), (0, "a" * 64), (1, "invalid")])
def test_bad_expectation_never_touches_missing_storage(
    tmp_path: Path, generation: Any, content: str
) -> None:
    root = tmp_path / "absent"
    with pytest.raises(RotationJournalError):
        BackupRotationJournal(root / "keyring.json").prepare(
            request_id=REQ, expected_generation=generation, expected_source_hash=content
        )
    assert not root.exists()


def test_status_absent_never_creates_lock_or_directory(rotation_directory: Path) -> None:
    target, source, api = setup(rotation_directory)
    before = set(rotation_directory.iterdir())
    result = api.status(request_id=REQ)
    assert result.state == "absent" and not result.durability_confirmed
    assert result.observed_keyring_hash == sha256(source)
    assert before == set(rotation_directory.iterdir()) and target.read_bytes() == source


@pytest.mark.skipif(os.name != "nt", reason="native Windows capability")
def test_windows_prepare_refuses_before_creating_anything(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    with pytest.raises(RotationJournalError, match="POSIX"):
        BackupRotationJournal(root / "keyring.json").prepare(
            request_id=REQ, expected_generation=1, expected_source_hash="a" * 64
        )
    assert not root.exists()


@POSIX
def test_prepare_retry_and_readonly_status_leave_main_keyring_unchanged(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, api = prepared(rotation_directory)
    original_inode = target.stat().st_ino
    values = {
        p.name: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes())
        for p in directory(target).iterdir()
    }
    assert api.prepare(
        request_id=REQ, expected_generation=1, expected_source_hash=sha256(source)
    ).durability_confirmed
    assert target.stat().st_ino == original_inode and target.read_bytes() == source

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("readonly inspection must not mutate or generate entropy")

    monkeypatch.setattr(os, "urandom", forbidden)
    monkeypatch.setattr(os, "mkdir", forbidden)
    monkeypatch.setattr(os, "fsync", forbidden)
    monkeypatch.setattr(ProcessLock, "acquire", forbidden)
    status = api.status(request_id=REQ)
    assert status.state == "prepared_recorded" and not status.durability_confirmed
    assert status.target_generation == 2
    assert values == {
        p.name: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes())
        for p in directory(target).iterdir()
    }
    assert "key_b64" not in repr(status) + json.dumps(asdict(status))


@POSIX
def test_private_permissions_and_legacy_publication_guard(rotation_directory: Path) -> None:
    target, source, api = prepared(rotation_directory)
    assert stat.S_IMODE(directory(target).stat().st_mode) == 0o700
    for entry in directory(target).iterdir():
        assert stat.S_IMODE(entry.stat().st_mode) == 0o600 and entry.stat().st_nlink == 1
    with pytest.raises(BackupKeyUnavailableError, match="journal exists"):
        BackupKeyring.rotate(target)
    assert target.read_bytes() == source
    assert api.status(request_id=REQ).state == "prepared_recorded"


@POSIX
@pytest.mark.parametrize("fault", ["source-hash", "generation", "another-request"])
def test_conflicting_requests_do_not_allocate_new_target(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    target, source, api = prepared(rotation_directory)
    entries = {p.name: p.read_bytes() for p in directory(target).iterdir()}

    def no_entropy(size: int) -> bytes:
        raise AssertionError("conflicts must not allocate new key bytes")

    monkeypatch.setattr(os, "urandom", no_entropy)
    with pytest.raises(RotationJournalError):
        api.prepare(
            request_id="b" * 32 if fault == "another-request" else REQ,
            expected_generation=2 if fault == "generation" else 1,
            expected_source_hash="0" * 64 if fault == "source-hash" else sha256(source),
        )
    assert target.read_bytes() == source
    assert entries == {p.name: p.read_bytes() for p in directory(target).iterdir()}


@POSIX
@pytest.mark.parametrize(
    "kind",
    [
        "directory-mode",
        "file-mode",
        "unknown-file",
        "nested-directory",
        "broken-json",
        "header-binding",
        "orphan-receipt",
        "hard-link",
        "symlink",
        "directory-symlink",
    ],
)
def test_corrupt_or_unsafe_catalog_is_not_repaired_or_published(
    rotation_directory: Path, kind: str
) -> None:
    target, source, api = prepared(rotation_directory)
    root = directory(target)
    header = root / "header.json"
    if kind == "directory-mode":
        root.chmod(0o777)
    if kind == "file-mode":
        header.chmod(0o644)
    if kind == "unknown-file":
        safe_write(root / ".pending-unknown", b"synthetic")
    if kind == "nested-directory":
        (root / "unexpected").mkdir()
    if kind == "broken-json":
        safe_write(header, b"not-json")
    if kind == "header-binding":
        safe_write(header, RotationHeader("b" * 32, "0" * 64).to_bytes())
    if kind == "orphan-receipt":
        safe_write(root / ("0" * 64 + ".accepted.json"), b"{}")
    if kind == "hard-link":
        os.link(header, rotation_directory / "alias")
    if kind == "symlink":
        saved = rotation_directory / "saved-header"
        header.rename(saved)
        header.symlink_to(saved)
    if kind == "directory-symlink":
        saved = rotation_directory / "saved-journal"
        root.rename(saved)
        root.symlink_to(saved, target_is_directory=True)
    try:
        with pytest.raises(RotationJournalError):
            api.status(request_id=REQ)
        with pytest.raises(RotationJournalError):
            api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
        assert target.read_bytes() == source
    finally:
        if kind == "directory-mode":
            root.chmod(0o700)


@POSIX
@pytest.mark.parametrize(
    "stage", ["parent-directory-sync", "header-sync", "intent-sync", "confirmation"]
)
def test_sync_fault_is_unknown_and_retry_reuses_same_visible_intent(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    target, source, api = setup(rotation_directory)
    import noyra.core.backup_rotation_storage as storage_module

    original = getattr(storage_module, "_fsync_directory", None)
    assert callable(original)
    hit = False

    def fail(path: Path) -> None:
        nonlocal hit
        root = directory(target)
        intents = list(root.glob("*.intent.json")) if root.exists() else []
        headers = list(root.glob("header.json")) if root.exists() else []
        selected = (
            (stage == "parent-directory-sync" and path == target.parent)
            or (stage == "header-sync" and headers and not intents and path == root)
            or (stage == "intent-sync" and intents and path == root)
        )
        if selected and not hit:
            hit = True
            raise OSError("synthetic directory-sync fault")
        original(path)

    with monkeypatch.context() as patch:
        if stage == "confirmation":
            method = RotationStorage.confirm

            def confirm(self: RotationStorage, name: str) -> None:
                nonlocal hit
                if name.endswith(".intent.json"):
                    hit = True
                    raise OSError("synthetic confirmation fault")
                method(self, name)

            patch.setattr(RotationStorage, "confirm", confirm)
        else:
            patch.setattr(storage_module, "_fsync_directory", fail)
        with pytest.raises(RotationPreparationUnknownError):
            api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    assert hit and target.read_bytes() == source
    intent_files = list(directory(target).glob("*.intent.json"))
    previous = intent_files[0].read_bytes() if intent_files else None
    result = api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    assert result.state == "prepared" and result.durability_confirmed
    if previous is not None:
        assert next(directory(target).glob("*.intent.json")).read_bytes() == previous
    assert target.read_bytes() == source


@POSIX
def test_acceptance_contract_read_only_observation_and_regression_detection(
    rotation_directory: Path,
) -> None:
    target, source, api = prepared(rotation_directory)
    file = next(directory(target).glob("*.intent.json"))
    raw = file.read_bytes()
    intent = RotationIntent.from_bytes(raw)
    # Synthetic later publication fixture; the prepare API never performs this.
    safe_write(target, intent.target)
    assert api.status(request_id=REQ).state == "target_visible_unconfirmed"
    receipt = RotationAcceptance(sha256(raw), intent.target_hash, REQ)
    safe_write(file.with_name(file.name.replace(".intent.", ".accepted.")), receipt.to_bytes())
    result = api.status(request_id=REQ)
    assert result.state == "accepted_recorded" and not result.durability_confirmed
    safe_write(target, source)
    assert api.status(request_id=REQ).state == "conflict"
    with pytest.raises(RotationJournalError, match="regressed"):
        api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))


@POSIX
def test_location_can_read_new_revision_without_resetting_pinned_boundary(
    rotation_directory: Path,
) -> None:
    target, source, _ = setup(rotation_directory)
    location = BackupRotationLocation(target, permission_error=_keyring_permission_error)
    pinned = BackupRotationBoundary(target, permission_error=_keyring_permission_error)
    other = rotation_directory / "other"
    BackupKeyring.initialize(other)
    replacement = other.read_bytes()
    other.replace(target)
    assert location.read_current(1024 * 1024) == replacement
    with pytest.raises(ValueError, match="identity changed"):
        pinned.read(1024 * 1024)
    assert replacement != source


@POSIX
def test_preparation_rejects_real_process_lock_contention(rotation_directory: Path) -> None:
    target, source, api = setup(rotation_directory)
    lock = ProcessLock(target.with_name(target.name + ".rotation.lock"))
    lock.acquire()
    script = """
import sys
sys.path.insert(0,sys.argv[1])
from noyra.core.backup_rotation import BackupRotationJournal
BackupRotationJournal(sys.argv[2]).prepare(request_id='a'*32,expected_generation=1,
                                          expected_source_hash=sys.argv[3])
"""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                script,
                str(Path(__file__).resolve().parents[1] / "src"),
                str(target),
                sha256(source),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0 and "RuntimeOwnershipError" in result.stderr
    finally:
        lock.release()
    assert target.read_bytes() == source and not directory(target).exists()
    assert (
        api.prepare(
            request_id=REQ, expected_generation=1, expected_source_hash=sha256(source)
        ).state
        == "prepared"
    )


@POSIX
@pytest.mark.parametrize("budget", ["entries", "total-bytes", "metadata-bytes"])
def test_catalog_work_limits_fail_without_publishing(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    target, source, api = prepared(rotation_directory)
    if budget == "entries":
        monkeypatch.setattr("noyra.core.backup_rotation_storage.MAX_ENTRIES", 1)
    elif budget == "total-bytes":
        monkeypatch.setattr("noyra.core.backup_rotation_storage.MAX_TOTAL_BYTES", 1)
    else:
        safe_write(directory(target) / "header.json", b"x" * 4097)
    with pytest.raises(RotationJournalError, match="budget"):
        api.status(request_id=REQ)
    assert target.read_bytes() == source


@POSIX
def test_create_only_publication_does_not_overwrite_unexpected_final_file(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, api = setup(rotation_directory)
    original = os.link
    foreign = b"synthetic-foreign-content"

    def link(source_path: Any, destination: Any, **kwargs: Any) -> None:
        if str(destination).endswith(".intent.json"):
            safe_write(Path(destination), foreign)
        original(source_path, destination, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", link)
        with pytest.raises(RotationPreparationUnknownError):
            api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    file = next(directory(target).glob("*.intent.json"))
    assert file.read_bytes() == foreign and target.read_bytes() == source
    assert not list(directory(target).glob(".pending-*"))
    with pytest.raises(RotationJournalError):
        api.status(request_id=REQ)


@POSIX
def test_catalog_replacement_is_not_followed(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, api = prepared(rotation_directory)
    original = RotationStorage.read
    replaced = False

    def read(self: RotationStorage, name: str) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            self.root.rename(rotation_directory / "saved")
            self.root.mkdir(mode=0o700)
            safe_write(self.root / "header.json", b"foreign")
        return original(self, name)

    monkeypatch.setattr(RotationStorage, "read", read)
    with pytest.raises(RotationJournalError, match="identity changed"):
        api.status(request_id=REQ)
    assert target.read_bytes() == source
    assert (directory(target) / "header.json").read_bytes() == b"foreign"


@POSIX
def test_record_open_identity_swap_is_rejected(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, api = prepared(rotation_directory)
    header = directory(target) / "header.json"
    original = os.open
    swapped = False

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if Path(path) == header and not swapped:
            swapped = True
            other = rotation_directory / "replacement"
            safe_write(other, header.read_bytes())
            other.replace(header)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opened)
    with pytest.raises(RotationJournalError, match="before reading"):
        api.status(request_id=REQ)
    assert swapped and target.read_bytes() == source


@POSIX
def test_source_mutation_before_preparation_commit_is_detected(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, api = setup(rotation_directory)
    original = RotationStorage.create

    def create(self: RotationStorage, name: str, raw: bytes, *, request_id: str) -> None:
        if name.endswith(".intent.json"):
            with target.open("ab") as stream:
                stream.write(b" ")
        original(self, name, raw, request_id=request_id)

    monkeypatch.setattr(RotationStorage, "create", create)
    with pytest.raises(RotationJournalError, match="unsafe"):
        api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    assert target.read_bytes() == source + b" "
    assert not list(directory(target).glob("*.intent.json"))


@POSIX
def test_prepared_retry_does_not_generate_any_randomness(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, api = prepared(rotation_directory)

    def forbidden(length: int) -> bytes:
        raise AssertionError("prepared retry must reuse stored target")

    monkeypatch.setattr(os, "urandom", forbidden)
    value = api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    assert value.state == "prepared" and target.read_bytes() == source


@POSIX
def test_future_accepted_chain_contract_and_prepare_followup(rotation_directory: Path) -> None:
    target, source, api = prepared(rotation_directory)
    first_file = next(directory(target).glob("*.intent.json"))
    first_raw = first_file.read_bytes()
    first = RotationIntent.from_bytes(first_raw)
    safe_write(
        first_file.with_name(first_file.name.replace(".intent.", ".accepted.")),
        RotationAcceptance(sha256(first_raw), first.target_hash, REQ).to_bytes(),
    )
    safe_write(target, first.target)
    result = api.prepare(
        request_id="b" * 32, expected_generation=2, expected_source_hash=sha256(first.target)
    )
    assert result.target_generation == 3 and target.read_bytes() == first.target
    assert api.status(request_id=REQ).state == "accepted_recorded"
    assert api.status(request_id="b" * 32).state == "prepared_recorded"
    assert source != first.target


@POSIX
def test_real_process_exit_after_prepared_file_reuses_target(rotation_directory: Path) -> None:
    target, source, api = setup(rotation_directory)
    script = """
import os,sys
sys.path.insert(0,sys.argv[1])
from noyra.core.backup_rotation import BackupRotationJournal
from noyra.core.backup_rotation_storage import RotationStorage
original=RotationStorage.create
def create(self,name,raw,*,request_id):
    original(self,name,raw,request_id=request_id)
    if name.endswith('.intent.json'):os._exit(91)
RotationStorage.create=create
BackupRotationJournal(sys.argv[2]).prepare(request_id='a'*32,expected_generation=1,
                                          expected_source_hash=sys.argv[3])
"""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            str(Path(__file__).resolve().parents[1] / "src"),
            str(target),
            sha256(source),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 91, result.stderr
    stored = next(directory(target).glob("*.intent.json")).read_bytes()
    assert api.status(request_id=REQ).state == "prepared_recorded"
    assert (
        api.prepare(
            request_id=REQ, expected_generation=1, expected_source_hash=sha256(source)
        ).target_hash
        == RotationIntent.from_bytes(stored).target_hash
    )
    assert next(directory(target).glob("*.intent.json")).read_bytes() == stored
    assert target.read_bytes() == source


@POSIX
def test_known_crash_orphan_is_retained_and_blocks_automatic_preparation(
    rotation_directory: Path,
) -> None:
    target, source, api = prepared(rotation_directory)
    pending = directory(target) / ".pending-synthetic"
    safe_write(pending, b"synthetic-orphan")
    with pytest.raises(RotationJournalError, match="incomplete"):
        api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    assert pending.read_bytes() == b"synthetic-orphan" and target.read_bytes() == source


@POSIX
def test_root_service_group_source_is_unchanged_and_journal_is_owner_only(
    rotation_directory: Path,
) -> None:
    uid = getattr(os, "geteuid", None)
    chown = getattr(os, "chown", None)
    assert callable(uid) and callable(chown)
    if uid() != 0:
        pytest.skip("actual root-owned service-group fixture")
    target, source, api = setup(rotation_directory)
    chown(target, 0, 65534)
    target.chmod(0o640)
    result = api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    assert result.state == "prepared"
    value = target.stat()
    assert (value.st_uid, value.st_gid, stat.S_IMODE(value.st_mode)) == (0, 65534, 0o640)
    assert target.read_bytes() == source and stat.S_IMODE(directory(target).stat().st_mode) == 0o700


def manual_catalog(target: Path, source: bytes) -> RotationIntent:
    from noyra.core.backup_rotation import _target

    root = directory(target)
    root.mkdir(mode=0o700)
    header = RotationHeader("b" * 32, sha256(str(target.resolve()).encode("utf-8")))
    safe_write(root / "header.json", header.to_bytes())
    new = _target(source)
    item = RotationIntent(
        REQ, header.slot_id, sha256(header.to_bytes()), 1, sha256(source), sha256(new), source, new
    )
    safe_write(root / item.filename, item.to_bytes())
    if os.name == "nt":
        from noyra.core.at_rest import _windows_harden

        _windows_harden(root, entire_tree=True)
    return item


def test_manual_valid_catalog_can_be_inspected_without_publication(
    rotation_directory: Path,
) -> None:
    target, source, api = setup(rotation_directory)
    item = manual_catalog(target, source)
    result = api.status(request_id=REQ)
    assert result.state == "prepared_recorded" and result.target_hash == item.target_hash
    assert result.target_generation == 2 and not result.durability_confirmed
    assert target.read_bytes() == source
    assert not target.with_name(target.name + ".rotation.lock").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows journal ACL")
def test_windows_status_rejects_journal_grants_to_other_accounts(rotation_directory: Path) -> None:
    import base64

    from noyra.core.at_rest import _powershell_command

    target, source, api = setup(rotation_directory)
    item = manual_catalog(target, source)
    path = directory(target) / item.filename
    encoded = base64.b64encode(str(path).encode("utf-8")).decode("ascii")
    script = f"""
$ErrorActionPreference='Stop'
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))
$item=Get-Item -LiteralPath $p -Force
$acl=$item.GetAccessControl()
$sid=[System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$rule=[System.Security.AccessControl.FileSystemAccessRule]::new(
  $sid,[System.Security.AccessControl.FileSystemRights]::ReadData,
  [System.Security.AccessControl.AccessControlType]::Allow)
$acl.AddAccessRule($rule)
$item.SetAccessControl($acl)
"""
    subprocess.run(_powershell_command(script), check=True, capture_output=True)
    with pytest.raises(RotationJournalError, match="ACL"):
        api.status(request_id=REQ)
    assert target.read_bytes() == source


def test_legacy_rotation_does_not_bypass_broken_journal_link(rotation_directory: Path) -> None:
    target, source, _ = setup(rotation_directory)
    try:
        directory(target).symlink_to(rotation_directory / "missing", target_is_directory=True)
    except OSError:
        pytest.skip("native symlinks unavailable")
    with pytest.raises(BackupKeyUnavailableError, match="journal exists"):
        BackupKeyring.rotate(target)
    assert target.read_bytes() == source


@POSIX
def test_file_write_fault_closes_descriptor_and_cleans_only_owned_temporary(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, api = setup(rotation_directory)
    observed: list[int] = []

    def broken(descriptor: int, payload: bytes) -> None:
        observed.append(descriptor)
        os.write(descriptor, payload[:1])
        raise OSError("synthetic partial write")

    with monkeypatch.context() as patch:
        patch.setattr("noyra.core.backup_rotation_storage._write_descriptor", broken)
        with pytest.raises(RotationPreparationUnknownError):
            api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    assert observed and not list(directory(target).iterdir())
    for descriptor in observed:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert target.read_bytes() == source
    assert (
        api.prepare(
            request_id=REQ, expected_generation=1, expected_source_hash=sha256(source)
        ).state
        == "prepared"
    )


@POSIX
def test_unrelated_source_hash_cannot_be_spliced_into_accepted_chain(
    rotation_directory: Path,
) -> None:
    from noyra.core.backup_rotation import _target

    target, source, api = prepared(rotation_directory)
    first_file = next(directory(target).glob("*.intent.json"))
    first_raw = first_file.read_bytes()
    first = RotationIntent.from_bytes(first_raw)
    safe_write(
        first_file.with_name(first_file.name.replace(".intent.", ".accepted.")),
        RotationAcceptance(sha256(first_raw), first.target_hash, REQ).to_bytes(),
    )
    alien_source = first.target + b" "
    new = _target(alien_source)
    second = RotationIntent(
        "b" * 32,
        first.slot_id,
        first.header_hash,
        2,
        sha256(alien_source),
        sha256(new),
        alien_source,
        new,
    )
    safe_write(directory(target) / second.filename, second.to_bytes())
    with pytest.raises(RotationJournalError, match="predecessor"):
        api.status(request_id=REQ)
    assert target.read_bytes() == source


@POSIX
def test_retained_key_limit_is_preflighted_before_creating_journal(
    rotation_directory: Path,
) -> None:
    from test_backup_rotation_records import snapshot

    target, _, api = setup(rotation_directory)
    full = snapshot(64)
    safe_write(target, full)
    with pytest.raises(RotationJournalError, match="retained-key limit"):
        api.prepare(request_id=REQ, expected_generation=64, expected_source_hash=sha256(full))
    assert not directory(target).exists() and target.read_bytes() == full


@POSIX
def test_temporary_identity_failure_preserves_empty_orphan_and_closes_descriptor(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, api = setup(rotation_directory)
    original_open, original_fstat = os.open, os.fstat
    temporary_fd: int | None = None
    failed = False

    def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal temporary_fd
        value = original_open(path, flags, *args, **kwargs)
        if Path(path).name.startswith(".pending-"):
            temporary_fd = value
        return value

    def fstat(descriptor: int) -> os.stat_result:
        nonlocal failed
        if descriptor == temporary_fd and not failed:
            failed = True
            raise OSError("synthetic temporary identity failure")
        return original_fstat(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opened)
        patch.setattr(os, "fstat", fstat)
        with pytest.raises(RotationPreparationUnknownError):
            api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    assert failed and temporary_fd is not None and target.read_bytes() == source
    with pytest.raises(OSError):
        original_fstat(temporary_fd)
    leftovers = list(directory(target).iterdir())
    assert len(leftovers) == 1 and leftovers[0].read_bytes() == b""
    with pytest.raises(RotationJournalError, match="incomplete"):
        api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))


@POSIX
def test_next_prepare_cannot_extend_an_accepted_chain_from_a_different_source(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, api = prepared(rotation_directory)
    first_file = next(directory(target).glob("*.intent.json"))
    raw = first_file.read_bytes()
    first = RotationIntent.from_bytes(raw)
    safe_write(
        first_file.with_name(first_file.name.replace(".intent.", ".accepted.")),
        RotationAcceptance(sha256(raw), first.target_hash, REQ).to_bytes(),
    )
    different = first.target + b" "
    safe_write(target, different)
    before = {p.name: p.read_bytes() for p in directory(target).iterdir()}

    def forbidden(length: int) -> bytes:
        raise AssertionError("invalid predecessor must fail before new target allocation")

    monkeypatch.setattr(os, "urandom", forbidden)
    with pytest.raises(RotationJournalError, match="predecessor"):
        api.prepare(
            request_id="b" * 32, expected_generation=2, expected_source_hash=sha256(different)
        )
    assert before == {p.name: p.read_bytes() for p in directory(target).iterdir()}
    assert target.read_bytes() == different and source != different


@POSIX
@pytest.mark.parametrize("same_request", [True, False], ids=["same-request", "other-request"])
def test_two_real_preparation_processes_are_serialized(
    rotation_directory: Path, same_request: bool
) -> None:
    import time

    target, source, api = setup(rotation_directory)
    entered, release = rotation_directory / "entered", rotation_directory / "release"
    script = """
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from noyra.core.backup_rotation import BackupRotationJournal
from noyra.core.backup_rotation_storage import RotationStorage
original=RotationStorage.create
entered,release=Path(sys.argv[4]),Path(sys.argv[5])
def create(self,name,raw,*,request_id):
    original(self,name,raw,request_id=request_id)
    if name.endswith('.intent.json'):
        entered.write_text('ready')
        deadline=time.monotonic()+30
        while not release.exists():
            if time.monotonic()>deadline:raise TimeoutError('synthetic test schedule')
            time.sleep(.02)
RotationStorage.create=create
BackupRotationJournal(sys.argv[2]).prepare(request_id='a'*32,expected_generation=1,
                                          expected_source_hash=sys.argv[3])
"""
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    owner = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            source_root,
            str(target),
            sha256(source),
            str(entered),
            str(release),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not entered.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists()
        with pytest.raises(RuntimeOwnershipError, match="another process owns runtime lock"):
            api.prepare(
                request_id=REQ if same_request else "b" * 32,
                expected_generation=1,
                expected_source_hash=sha256(source),
            )
    finally:
        release.write_text("release", encoding="utf-8")
        _, stderr = owner.communicate(timeout=35)
    assert owner.returncode == 0, stderr
    stored = next(directory(target).glob("*.intent.json")).read_bytes()
    if same_request:
        result = api.prepare(
            request_id=REQ, expected_generation=1, expected_source_hash=sha256(source)
        )
        assert result.target_hash == RotationIntent.from_bytes(stored).target_hash
    else:
        with pytest.raises(RotationJournalError, match="pending"):
            api.prepare(
                request_id="b" * 32, expected_generation=1, expected_source_hash=sha256(source)
            )
    assert next(directory(target).glob("*.intent.json")).read_bytes() == stored
    assert target.read_bytes() == source


@POSIX
def test_new_record_total_budget_is_checked_before_private_file_creation(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, api = setup(rotation_directory)
    import noyra.core.backup_rotation as facade

    original = getattr(facade, "_target", None)
    assert callable(original)

    def allocate(raw: bytes) -> bytes:
        monkeypatch.setattr("noyra.core.backup_rotation_storage.MAX_TOTAL_BYTES", 1)
        value = original(raw)
        assert isinstance(value, bytes)
        return value

    monkeypatch.setattr(facade, "_target", allocate)
    with pytest.raises(RotationJournalError, match="budget"):
        api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    assert target.read_bytes() == source
    assert {p.name for p in directory(target).iterdir()} == {"header.json"}


@POSIX
def test_preparation_preserves_old_backup_restore_and_does_not_export_key_journal(
    rotation_directory: Path,
) -> None:
    from noyra.core.at_rest import EncryptedBackupManager
    from noyra.core.database import Database
    from noyra.core.identity import IdentityStore
    from noyra.core.types import content_hash

    target, source, api = setup(rotation_directory)
    data = rotation_directory / "data"
    data.mkdir(mode=0o700)
    database = Database(data / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-journal-fixture", content_hash("synthetic"))
    manager = EncryptedBackupManager(data, target)
    first = manager.create(rotation_directory / "before.backup")
    api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    second = manager.create(rotation_directory / "after.backup")
    assert (
        first.key_id == second.key_id and first.keyring_generation == second.keyring_generation == 1
    )
    for label, artifact in [("before", first), ("after", second)]:
        restored = manager.restore(artifact.path, rotation_directory / ("restore-" + label))
        assert (restored / "noyra.sqlite3").is_file()
        assert not list(restored.rglob("*.rotation-journal"))
    assert target.read_bytes() == source
    assert api.status(request_id=REQ).state == "prepared_recorded"
