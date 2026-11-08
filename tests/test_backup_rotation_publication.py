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

from noyra.core.at_rest import BackupKeyring, _keyring_permission_error
from noyra.core.backup_keyring_paths import BackupRotationLocation
from noyra.core.backup_rotation import BackupRotationJournal
from noyra.core.backup_rotation_records import (
    RotationAcceptance,
    RotationConflictError,
    RotationIntent,
    RotationOutcomeUnknownError,
    RotationUnsupportedError,
    sha256,
)
from noyra.core.backup_rotation_target import staging_path
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.locking import ProcessLock

POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX durable publication")
REQ = "a" * 32


def prepared(root: Path) -> tuple[Path, bytes, BackupRotationJournal]:
    target = root / "keyring.json"
    BackupKeyring.initialize(target)
    source = target.read_bytes()
    api = BackupRotationJournal(target)
    api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    return target, source, api


def publication_stage(target: Path) -> Path:
    location = BackupRotationLocation(target, permission_error=_keyring_permission_error)
    return staging_path(location, intent_at(target))


def directory(target: Path) -> Path:
    return target.with_name("." + target.name + ".rotation-journal")


def intent_at(target: Path) -> RotationIntent:
    files = sorted(directory(target).glob("*.intent.json"))
    return RotationIntent.from_bytes(files[0].read_bytes())


def test_resume_requires_existing_request_without_creating_storage(tmp_path: Path) -> None:
    import pytest

    from noyra.core.backup_rotation_records import RotationJournalError

    missing = tmp_path / "missing" / "keyring.json"
    with pytest.raises(RotationJournalError):
        BackupRotationJournal(missing).resume(request_id="a" * 32)
    assert not missing.parent.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows capability boundary")
def test_windows_resume_refuses_before_lock_or_directory_creation(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    with pytest.raises(RotationUnsupportedError, match="POSIX"):
        BackupRotationJournal(root / "keyring.json").resume(request_id=REQ)
    assert not root.exists()


@POSIX
def test_resume_and_repeat_publish_exactly_the_prepared_target_once(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, api = prepared(rotation_directory)
    before = BackupKeyring.from_path(target)
    intent = intent_at(target)

    def no_entropy(length: int) -> bytes:
        raise AssertionError("resume must not allocate another key or random staging name")

    monkeypatch.setattr(os, "urandom", no_entropy)
    result = api.resume(request_id=REQ)
    assert result.state == "accepted" and result.durability_confirmed
    assert result.generation == 2 and result.profile is not None
    assert target.read_bytes() == intent.target
    first_inode = target.stat().st_ino
    assert target.read_bytes() != source
    assert BackupKeyring.from_path(target).resolve(before.active.key_id, before.active.fingerprint)
    repeated = api.resume(request_id=REQ)
    assert repeated == result and target.stat().st_ino == first_inode
    assert api.status(request_id=REQ).state == "accepted_recorded"
    assert not list(directory(target).glob("*.tmp"))
    assert not list(target.parent.glob(".noyra-rotation-*.publish.tmp"))
    assert "key_b64" not in repr(result) + json.dumps(asdict(result))


@POSIX
def test_missing_request_does_not_implicitly_prepare(rotation_directory: Path) -> None:
    target = rotation_directory / "keyring.json"
    BackupKeyring.initialize(target)
    source = target.read_bytes()
    api = BackupRotationJournal(target)
    with pytest.raises(RotationConflictError, match="no preparation"):
        api.resume(request_id=REQ)
    assert not directory(target).exists() and target.read_bytes() == source
    api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    with pytest.raises(RotationConflictError, match="no existing intent"):
        api.resume(request_id="b" * 32)
    assert target.read_bytes() == source


@POSIX
def test_old_request_never_reverts_a_later_accepted_generation(rotation_directory: Path) -> None:
    target, _, api = prepared(rotation_directory)
    api.resume(request_id=REQ)
    next_source = target.read_bytes()
    api.prepare(
        request_id="b" * 32, expected_generation=2, expected_source_hash=sha256(next_source)
    )
    api.resume(request_id="b" * 32)
    later = target.read_bytes()
    inode = target.stat().st_ino
    old = api.resume(request_id=REQ)
    assert old.state == "accepted_superseded" and old.generation == 3
    assert old.target_generation == 2 and old.durability_confirmed
    assert target.read_bytes() == later and target.stat().st_ino == inode
    assert BackupKeyring.from_path(target).generation == 3


@POSIX
def test_accepted_rollback_and_same_generation_different_bytes_are_conflicts(
    rotation_directory: Path,
) -> None:
    target, source, api = prepared(rotation_directory)
    api.resume(request_id=REQ)
    for content in [source, target.read_bytes() + b" "]:
        target.write_bytes(content)
        with pytest.raises(RotationConflictError):
            api.resume(request_id=REQ)
        assert target.read_bytes() == content


@POSIX
@pytest.mark.parametrize(
    "where", ["publication-parent", "target-confirmation", "receipt-directory"]
)
def test_directory_sync_failure_is_unknown_and_retry_does_not_replace_visible_target(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    target, source, api = prepared(rotation_directory)
    import noyra.core.backup_rotation_publication as publication
    import noyra.core.backup_rotation_storage as storage

    original = getattr(publication, "_fsync_directory", None)
    assert callable(original)
    fired = False

    def fail(path: Path) -> None:
        nonlocal fired
        receipt = list(directory(target).glob("*.accepted.json"))
        selected = (
            (where == "publication-parent" and path == target.parent)
            or (where == "target-confirmation" and path == target.parent)
            or (where == "receipt-directory" and bool(receipt))
        )
        if selected and not fired:
            fired = True
            raise OSError("synthetic post-publication directory sync failure")
        original(path)

    with monkeypatch.context() as patch:
        if where == "receipt-directory":
            patch.setattr(storage, "_fsync_directory", fail)
        else:
            patch.setattr(publication, "_fsync_directory", fail)
        with pytest.raises(RotationOutcomeUnknownError) as caught:
            api.resume(request_id=REQ)
    assert fired and caught.value.request_id == REQ
    assert target.read_bytes() == intent_at(target).target and target.read_bytes() != source
    inode = target.stat().st_ino
    assert api.resume(request_id=REQ).state == "accepted"
    assert target.stat().st_ino == inode and BackupKeyring.from_path(target).generation == 2


@POSIX
def test_repeated_post_replace_failures_do_not_advance_again(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _, api = prepared(rotation_directory)
    import noyra.core.backup_rotation_publication as publication

    original = getattr(publication, "_fsync_directory", None)
    assert callable(original)

    def fail(path: Path) -> None:
        if path == target.parent:
            raise OSError("synthetic persistent sync failure")
        original(path)

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_fsync_directory", fail)
        for _ in range(5):
            with pytest.raises(RotationOutcomeUnknownError):
                api.resume(request_id=REQ)
            assert BackupKeyring.from_path(target).generation == 2
    assert api.resume(request_id=REQ).state == "accepted"


@POSIX
@pytest.mark.parametrize("kind", ["publish", "accept"])
def test_partial_known_stage_is_resumed_but_mismatched_bytes_are_not_deleted(
    rotation_directory: Path, kind: str
) -> None:
    target, source, api = prepared(rotation_directory)
    intent = intent_at(target)
    stem = intent.filename.removesuffix(".intent.json")
    if kind == "publish":
        content = intent.target
    else:
        target.write_bytes(intent.target)
        intent_raw = (directory(target) / intent.filename).read_bytes()
        content = RotationAcceptance(sha256(intent_raw), intent.target_hash, REQ).to_bytes()
    stage = (
        publication_stage(target)
        if kind == "publish"
        else directory(target) / (stem + "." + kind + ".tmp")
    )
    stage.write_bytes(content[:12])
    stage.chmod(0o600)
    assert api.resume(request_id=REQ).state == "accepted" and not stage.exists()
    assert target.read_bytes() == intent.target
    assert source != intent.target


@POSIX
def test_foreign_or_hardlinked_stage_is_not_overwritten_or_removed(
    rotation_directory: Path,
) -> None:
    target, source, api = prepared(rotation_directory)
    intent = intent_at(target)
    stage = publication_stage(target)
    stage.write_bytes(b"synthetic-foreign-data")
    stage.chmod(0o600)
    with pytest.raises(RotationConflictError):
        api.resume(request_id=REQ)
    assert stage.read_bytes() == b"synthetic-foreign-data" and target.read_bytes() == source
    stage.unlink()
    stage.write_bytes(intent.target)
    stage.chmod(0o600)
    alias = rotation_directory / "external-alias"
    os.link(stage, alias)
    with pytest.raises(RotationConflictError):
        api.resume(request_id=REQ)
    assert alias.read_bytes() == intent.target and target.read_bytes() == source


@POSIX
def test_os_replace_exception_after_effect_is_recoverable_without_second_replace(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _, api = prepared(rotation_directory)
    original = os.replace

    def replace(source: Any, destination: Any, **kwargs: Any) -> None:
        original(source, destination, **kwargs)
        if Path(destination) == target:
            raise OSError("synthetic replace-after-effect failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        with pytest.raises(RotationOutcomeUnknownError):
            api.resume(request_id=REQ)
    inode = target.stat().st_ino
    assert api.resume(request_id=REQ).state == "accepted" and target.stat().st_ino == inode


@POSIX
def test_target_mutation_before_acceptance_never_reports_success(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _, api = prepared(rotation_directory)

    def mutate(name: str) -> None:
        if name == "target_confirmed":
            with target.open("ab") as stream:
                stream.write(b" ")

    monkeypatch.setattr("noyra.core.backup_rotation_publication.checkpoint", mutate)
    with pytest.raises(RotationConflictError):
        api.resume(request_id=REQ)
    assert not list(directory(target).glob("*.accepted.json"))


@POSIX
def test_resume_uses_real_existing_process_lock(rotation_directory: Path) -> None:
    target, source, api = prepared(rotation_directory)
    lock = ProcessLock(target.with_name(target.name + ".rotation.lock"))
    lock.acquire()
    try:
        with pytest.raises(RuntimeOwnershipError, match="another process owns runtime lock"):
            api.resume(request_id=REQ)
    finally:
        lock.release()
    assert target.read_bytes() == source
    assert api.resume(request_id=REQ).state == "accepted"


CHECKPOINTS = [
    "preparation_confirmed",
    "target_stage_synced",
    "before_target_replace",
    "target_replaced",
    "target_parent_synced",
    "target_stage_directory_synced",
    "target_file_synced",
    "target_confirmed",
    "acceptance_stage_synced",
    "acceptance_linked",
    "acceptance_alias_removed",
    "acceptance_confirmed",
    "accepted_response",
]
IO_ROLES = [
    "sync:header",
    "sync:intent",
    "sync:publish",
    "sync:target",
    "sync:accept",
    "sync:receipt",
    "sync:journal-directory",
    "sync:target-directory",
    "write:publish",
    "write:accept",
    "replace:target",
    "link:receipt",
    "unlink:accept",
]
FAULT_CASES = [
    (label, mode) for label in [*CHECKPOINTS, *IO_ROLES] for mode in ["before", "after"]
] + [("write:publish", "partial"), ("write:accept", "partial")]


@POSIX
@pytest.mark.parametrize(
    "label,mode", FAULT_CASES, ids=[f"{label}-{mode}" for label, mode in FAULT_CASES]
)
def test_fault_matrix_preserves_one_fixed_target_and_idempotent_resume(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch, label: str, mode: str
) -> None:
    target, _, api = prepared(rotation_directory)
    expected = intent_at(target).target
    import noyra.core.backup_rotation_publication as publication
    import noyra.core.backup_rotation_storage as store_module

    original_sync, original_replace, original_link, original_unlink = (
        os.fsync,
        os.replace,
        os.link,
        os.unlink,
    )
    original_write = getattr(store_module, "_write_descriptor", None)
    assert callable(original_write)
    fired = False
    trace: list[str] = []

    def role(descriptor: int) -> str:
        value = os.fstat(descriptor)
        candidates = [
            (target, "target"),
            (target.parent, "target-directory"),
            (directory(target), "journal-directory"),
        ]
        for file in [
            *directory(target).iterdir(),
            *target.parent.glob(".noyra-rotation-*.publish.tmp"),
        ]:
            name = file.name
            kind = (
                "header"
                if name == "header.json"
                else "intent"
                if name.endswith(".intent.json")
                else "publish"
                if name.endswith(".publish.tmp")
                else "accept"
                if name.endswith(".accept.tmp")
                else "receipt"
                if name.endswith(".accepted.json")
                else "other"
            )
            candidates.append((file, kind))
        for path, kind in candidates:
            try:
                meta = path.stat()
            except FileNotFoundError:
                continue
            if (meta.st_dev, meta.st_ino) == (value.st_dev, value.st_ino):
                return kind
        return "other"

    def event(name: str, action: Any) -> Any:
        nonlocal fired
        trace.append(name)
        selected = name == label and not fired
        if selected:
            fired = True
            if mode == "before":
                raise OSError("synthetic fault before effect")
        value = action()
        if selected:
            raise OSError("synthetic fault after effect")
        return value

    def sync(descriptor: int) -> None:
        event("sync:" + role(descriptor), lambda: original_sync(descriptor))

    def write(descriptor: int, raw: bytes) -> None:
        nonlocal fired
        name = "write:" + role(descriptor)
        if name == label and mode == "partial" and not fired:
            fired = True
            original_write(descriptor, raw[:13])
            raise OSError("synthetic partial write")
        event(name, lambda: original_write(descriptor, raw))

    def replace(source: Any, destination: Any, **kwargs: Any) -> None:
        event("replace:target", lambda: original_replace(source, destination, **kwargs))

    def link(source: Any, destination: Any, **kwargs: Any) -> None:
        event("link:receipt", lambda: original_link(source, destination, **kwargs))

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        event("unlink:accept", lambda: original_unlink(path, *args, **kwargs))

    def checkpoint(name: str) -> None:
        event(name, lambda: None)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", sync)
        patch.setattr(os, "replace", replace)
        patch.setattr(os, "link", link)
        patch.setattr(os, "unlink", unlink)
        patch.setattr(store_module, "_write_descriptor", write)
        patch.setattr("noyra.core.backup_rotation_target._write_descriptor", write)
        patch.setattr(publication, "checkpoint", checkpoint)
        with pytest.raises(RotationOutcomeUnknownError) as caught:
            api.resume(request_id=REQ)
    assert fired, trace
    assert caught.value.request_id == REQ
    visible = target.read_bytes()
    inode = target.stat().st_ino
    assert visible in [expected, intent_at(target).source]
    assert api.resume(request_id=REQ).state == "accepted"
    assert target.read_bytes() == expected and BackupKeyring.from_path(target).generation == 2
    if visible == expected:
        assert target.stat().st_ino == inode
    final_inode = target.stat().st_ino
    assert api.resume(request_id=REQ).state == "accepted" and target.stat().st_ino == final_inode
    assert not list(directory(target).glob("*.tmp"))
    assert not list(target.parent.glob(".noyra-rotation-*.publish.tmp"))


@POSIX
@pytest.mark.parametrize("cut", CHECKPOINTS)
def test_real_process_exit_at_each_publication_checkpoint_resumes_same_target(
    rotation_directory: Path, cut: str
) -> None:
    target, _, api = prepared(rotation_directory)
    expected = intent_at(target).target
    script = """
import os,sys
sys.path.insert(0,sys.argv[1])
from noyra.core.backup_rotation import BackupRotationJournal
import noyra.core.backup_rotation_publication as publication
def checkpoint(name):
    if name==sys.argv[3]:os._exit(91)
publication.checkpoint=checkpoint
BackupRotationJournal(sys.argv[2]).resume(request_id='a'*32)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            str(Path(__file__).resolve().parents[1] / "src"),
            str(target),
            cut,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 91, result.stderr
    observed = target.read_bytes()
    inode = target.stat().st_ino
    # An alias pair is evidence of the same published receipt, not an excuse
    # to weaken the regular-file checks for unrelated hard links.
    state = api.status(request_id=REQ)
    assert not state.durability_confirmed
    assert api.resume(request_id=REQ).state == "accepted"
    assert target.read_bytes() == expected and BackupKeyring.from_path(target).generation == 2
    if observed == expected:
        assert target.stat().st_ino == inode
    assert not list(directory(target).glob("*.tmp"))
    assert not list(target.parent.glob(".noyra-rotation-*.publish.tmp"))


@POSIX
def test_root_service_group_mode_and_old_backup_restore_survive_resume(
    rotation_directory: Path,
) -> None:
    uid, chown = getattr(os, "geteuid", None), getattr(os, "chown", None)
    assert callable(uid) and callable(chown)
    if uid() != 0:
        pytest.skip("actual root-owned service-group fixture")
    from noyra.core.at_rest import EncryptedBackupManager
    from noyra.core.database import Database
    from noyra.core.identity import IdentityStore
    from noyra.core.types import content_hash

    target = rotation_directory / "keyring.json"
    old = BackupKeyring.initialize(target)
    chown(target, 0, 65534)
    target.chmod(0o640)
    data = rotation_directory / "data"
    data.mkdir(mode=0o700)
    database = Database(data / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-publication-fixture", content_hash("synthetic"))
    manager = EncryptedBackupManager(data, target)
    first = manager.create(rotation_directory / "old.backup")
    api = BackupRotationJournal(target)
    api.prepare(
        request_id=REQ, expected_generation=1, expected_source_hash=sha256(target.read_bytes())
    )
    api.resume(request_id=REQ)
    meta = target.stat()
    assert (meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode)) == (0, 65534, 0o640)
    assert BackupKeyring.from_path(target).resolve(old.active.key_id, old.active.fingerprint)
    second = manager.create(rotation_directory / "new.backup")
    assert first.key_id != second.key_id
    for label, artifact in [("old", first), ("new", second)]:
        restored = manager.restore(artifact.path, rotation_directory / ("restore-" + label))
        assert (restored / "noyra.sqlite3").is_file()
        assert not list(restored.rglob("*.rotation-journal"))


@POSIX
@pytest.mark.parametrize("role", ["publish", "accept"])
def test_real_process_exit_during_stage_write_is_recoverable(
    rotation_directory: Path, role: str
) -> None:
    target, _, api = prepared(rotation_directory)
    expected = intent_at(target).target
    script = """
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from noyra.core.backup_rotation import BackupRotationJournal
import noyra.core.backup_rotation_storage as storage
import noyra.core.backup_rotation_target as target_staging
original=storage._write_descriptor
root=Path(sys.argv[2]).with_name('.'+Path(sys.argv[2]).name+'.rotation-journal')
def write(fd,raw):
    value=os.fstat(fd)
    stage=(next(root.parent.glob('.noyra-rotation-*.publish.tmp'),None) if sys.argv[3]=='publish'
           else next(root.glob('*.accept.tmp'),None))
    if stage is not None and stage.stat().st_ino==value.st_ino:
        original(fd,raw[:13]);os._exit(92)
    original(fd,raw)
storage._write_descriptor=write
target_staging._write_descriptor=write
BackupRotationJournal(sys.argv[2]).resume(request_id='a'*32)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            str(Path(__file__).resolve().parents[1] / "src"),
            str(target),
            role,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 92, result.stderr
    stage = (
        publication_stage(target)
        if role == "publish"
        else next(directory(target).glob("*.accept.tmp"))
    )
    assert len(stage.read_bytes()) == 13
    assert api.resume(request_id=REQ).state == "accepted"
    assert target.read_bytes() == expected and BackupKeyring.from_path(target).generation == 2


@POSIX
@pytest.mark.parametrize("same_request", [True, False], ids=["same-request", "different-request"])
def test_real_concurrent_resume_cannot_duplicate_or_bypass_the_existing_request(
    rotation_directory: Path, same_request: bool
) -> None:
    import time

    target, _, api = prepared(rotation_directory)
    entered, release = rotation_directory / "entered", rotation_directory / "release"
    script = """
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from noyra.core.backup_rotation import BackupRotationJournal
import noyra.core.backup_rotation_publication as publication
entered,release=Path(sys.argv[3]),Path(sys.argv[4])
def checkpoint(name):
    if name=='target_replaced':
        entered.write_text('ready')
        deadline=time.monotonic()+30
        while not release.exists():
            if time.monotonic()>deadline:raise TimeoutError('synthetic test schedule')
            time.sleep(.02)
publication.checkpoint=checkpoint
BackupRotationJournal(sys.argv[2]).resume(request_id='a'*32)
"""
    owner = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            str(Path(__file__).resolve().parents[1] / "src"),
            str(target),
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
        with pytest.raises(RuntimeOwnershipError):
            api.resume(request_id=REQ if same_request else "b" * 32)
    finally:
        release.write_text("release", encoding="utf-8")
        _, stderr = owner.communicate(timeout=35)
    assert owner.returncode == 0, stderr
    inode = target.stat().st_ino
    if same_request:
        assert api.resume(request_id=REQ).state == "accepted"
    else:
        with pytest.raises(RotationConflictError, match="no existing intent"):
            api.resume(request_id="b" * 32)
    assert target.stat().st_ino == inode and BackupKeyring.from_path(target).generation == 2


@POSIX
def test_post_replace_readback_unavailable_is_unknown_and_preserves_same_request(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core.backup_keyring_paths import BackupRotationPathError

    target, _, api = prepared(rotation_directory)
    original = BackupRotationLocation.read_current
    unreadable = False

    def checkpoint(name: str) -> None:
        nonlocal unreadable
        if name == "target_replaced":
            unreadable = True

    def read(self: BackupRotationLocation, maximum: int) -> bytes:
        if unreadable:
            raise BackupRotationPathError("synthetic readback unavailable")
        return original(self, maximum)

    with monkeypatch.context() as patch:
        patch.setattr("noyra.core.backup_rotation_publication.checkpoint", checkpoint)
        patch.setattr(BackupRotationLocation, "read_current", read)
        with pytest.raises(RotationOutcomeUnknownError) as caught:
            api.resume(request_id=REQ)
    assert caught.value.request_id == REQ and caught.value.observed_state == "unavailable"
    inode = target.stat().st_ino
    assert api.resume(request_id=REQ).state == "accepted" and target.stat().st_ino == inode


@POSIX
@pytest.mark.parametrize("kind", ["source", "parent", "lock", "stage"])
def test_last_boundary_identity_changes_do_not_publish_over_an_unexpected_file(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    parent = rotation_directory / "private"
    parent.mkdir(mode=0o700)
    target, source, api = prepared(parent)
    foreign = rotation_directory / "foreign.json"
    BackupKeyring.initialize(foreign)
    foreign_bytes = foreign.read_bytes()

    def checkpoint(name: str) -> None:
        if name != "before_target_replace":
            return
        if kind == "source":
            foreign.replace(target)
        elif kind == "parent":
            parent.rename(rotation_directory / "saved-private")
            parent.mkdir(mode=0o700)
            foreign.replace(target)
        elif kind == "lock":
            other = rotation_directory / "other-lock"
            other.write_bytes(b"synthetic")
            other.chmod(0o600)
            other.replace(target.with_name(target.name + ".rotation.lock"))
        else:
            stage = publication_stage(target)
            foreign.replace(stage)

    with monkeypatch.context() as patch:
        patch.setattr("noyra.core.backup_rotation_publication.checkpoint", checkpoint)
        with pytest.raises((RotationOutcomeUnknownError, RotationConflictError)):
            api.resume(request_id=REQ)
    if kind in {"source", "parent"}:
        assert target.read_bytes() == foreign_bytes
    else:
        assert target.read_bytes() == source
    if kind == "stage":
        assert publication_stage(target).read_bytes() == foreign_bytes


@POSIX
def test_unsafe_receipt_alias_cannot_hide_an_external_hardlink(rotation_directory: Path) -> None:
    target, _, api = prepared(rotation_directory)
    api.resume(request_id=REQ)
    file = next(directory(target).glob("*.accepted.json"))
    alias = file.with_name(file.name.replace(".accepted.json", ".accept.tmp"))
    os.link(file, alias)
    outside = rotation_directory / "external-receipt"
    os.link(file, outside)
    assert file.stat().st_nlink == 3
    before = target.read_bytes()
    with pytest.raises(RotationConflictError):
        api.resume(request_id=REQ)
    assert outside.exists() and alias.exists() and target.read_bytes() == before


@POSIX
@pytest.mark.parametrize("mode", [0o400, 0o600], ids=["owner-readonly", "owner-private"])
def test_original_owner_mode_survives_an_interrupted_publication(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    target, _, api = prepared(rotation_directory)
    # A mode-only administrative change after preparation does not change the
    # request's key bytes. The publication boundary pins this current metadata.
    target.chmod(mode)
    uid = getattr(os, "geteuid", None)
    if callable(uid) and uid() == 0 and mode == 0o400:
        with pytest.raises(RotationConflictError):
            api.resume(request_id=REQ)
        assert not list(target.parent.glob(".noyra-rotation-*.publish.tmp"))
        return

    def fail(name: str) -> None:
        if name == "before_target_replace":
            raise OSError("synthetic pre-replace interruption")

    with monkeypatch.context() as patch:
        patch.setattr("noyra.core.backup_rotation_publication.checkpoint", fail)
        with pytest.raises(RotationOutcomeUnknownError):
            api.resume(request_id=REQ)
    assert stat.S_IMODE(publication_stage(target).stat().st_mode) == mode
    assert api.resume(request_id=REQ).state == "accepted"
    assert stat.S_IMODE(target.stat().st_mode) == mode


@POSIX
def test_actual_root_service_group_can_read_new_key_but_not_private_journal() -> None:
    import tempfile

    uid, chown = getattr(os, "geteuid", None), getattr(os, "chown", None)
    assert callable(uid) and callable(chown)
    if uid() != 0:
        pytest.skip("actual root service-group read")
    with tempfile.TemporaryDirectory(prefix="noyra-d4b2-service-", dir="/var/tmp") as folder:
        root = Path(folder)
        chown(root, 0, 65534)
        root.chmod(0o750)
        target, _, api = prepared(root)
        chown(target, 0, 65534)
        target.chmod(0o640)
        api.resume(request_id=REQ)
        script = """
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from noyra.core.at_rest import BackupKeyring,validate_keyring_path
assert BackupKeyring.from_path(validate_keyring_path(sys.argv[2])).generation==2
try:list(Path(sys.argv[3]).iterdir())
except PermissionError:pass
else:raise AssertionError('service read group must not read journal snapshots')
"""
        subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                script,
                str(Path(__file__).resolve().parents[1] / "src"),
                str(target),
                str(directory(target)),
            ],
            capture_output=True,
            check=True,
            user=65534,
            group=65534,
            extra_groups=[],
        )


@POSIX
def test_acceptance_link_budget_is_checked_before_publishing_a_receipt(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _, api = prepared(rotation_directory)

    def checkpoint(name: str) -> None:
        if name == "acceptance_stage_synced":
            total = sum(file.stat().st_size for file in directory(target).iterdir())
            monkeypatch.setattr("noyra.core.backup_rotation_storage.MAX_TOTAL_BYTES", total)

    with monkeypatch.context() as patch:
        patch.setattr("noyra.core.backup_rotation_publication.checkpoint", checkpoint)
        with pytest.raises(RotationConflictError):
            api.resume(request_id=REQ)
    assert not list(directory(target).glob("*.accepted.json"))
    # Restore the work budget explicitly before resuming the same fixed target.
    monkeypatch.undo()
    assert api.resume(request_id=REQ).state == "accepted"


@POSIX
def test_lock_setup_failure_is_typed_and_cannot_publish(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, api = prepared(rotation_directory)

    def fail(self: ProcessLock) -> bool:
        raise OSError("synthetic lock setup failure")

    monkeypatch.setattr(ProcessLock, "acquire", fail)
    with pytest.raises(RotationConflictError, match="lock"):
        api.resume(request_id=REQ)
    assert target.read_bytes() == source


@POSIX
def test_lock_release_error_after_acceptance_keeps_the_fixed_request_retryable(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _, api = prepared(rotation_directory)
    original = ProcessLock.release

    def release(self: ProcessLock) -> None:
        original(self)
        raise OSError("synthetic unlock-after-effect failure")

    with monkeypatch.context() as patch:
        patch.setattr(ProcessLock, "release", release)
        with pytest.raises(RotationOutcomeUnknownError) as caught:
            api.resume(request_id=REQ)
    assert caught.value.observed_state == "lock_release_failed"
    inode = target.stat().st_ino
    assert api.resume(request_id=REQ).state == "accepted" and target.stat().st_ino == inode
    assert BackupKeyring.from_path(target).generation == 2
