from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, cast

import pytest

from noyra.cognition.execution import (
    ProjectExecutionError,
    ProjectExecutionLedger,
    ProjectWorkspace,
)
from noyra.core import Database, IdentityStore
from noyra.core.archive import ArchiveTransferQueue
from noyra.core.errors import IntegrityError
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.training_export import TrainingDatasetExporter
from noyra.core.types import content_hash
from noyra.service import ServiceSettings

_STORAGE_KEY = re.compile(r"^subject_[0-9a-f]{32}$")


def _identity(database: Database, subject_id: str) -> str:
    identities = IdentityStore(database)
    identities.ensure(subject_id, content_hash({"subject": subject_id}))
    return identities.storage_key(subject_id)


def test_subject_id_ingress_accepts_only_ascii_slug_and_rejects_device_names(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    identities = IdentityStore(database)
    for subject_id in ("../escape", "Noyra\\escape", "中文主体", "CON", "subject name"):
        with pytest.raises(ValueError):
            identities.ensure(subject_id, "a" * 64)
    settings = dict(
        data_dir=tmp_path,
        subject_id="../escape",
        genesis_hash="a" * 64,
    )
    with pytest.raises(ValueError):
        ServiceSettings(**cast(Any, settings))


def test_identity_mutation_and_lookup_ingress_rejects_invalid_subject_ids(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    identities = IdentityStore(database)
    invalid = "../escape"
    with pytest.raises(ValueError):
        identities.ensure("Noyra-valid-subject", "a" * 64, origin_subject_id=invalid)
    with pytest.raises(ValueError):
        identities.load(invalid)
    with pytest.raises(ValueError):
        identities.storage_key(invalid)
    with pytest.raises(ValueError):
        identities.legacy_storage_path_is_unambiguous(invalid)
    with pytest.raises(ValueError):
        identities.update_checkpoint(
            invalid,
            state_version=1,
            checkpoint_id="checkpoint-invalid-subject",
        )
    with pytest.raises(ValueError):
        identities.set_personal_name(invalid, "display")


def test_path_consumers_reject_invalid_subject_ids_at_construction(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    with pytest.raises(ValueError):
        ArchiveTransferQueue(database, "../escape", tmp_path / "training_raw")
    with pytest.raises(ValueError):
        ProjectExecutionLedger(database, "../escape")


def test_export_ingress_rejects_invalid_subject_ids_before_path_work(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    with pytest.raises(ValueError):
        RuntimeLogExporter(database).export("../escape", actor="test")
    with pytest.raises(ValueError):
        TrainingDatasetExporter(database, data_root=tmp_path).export("../escape", actor="test")


def test_storage_keys_are_random_immutable_and_subject_scoped(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    first = _identity(database, "Noyra-storage-a")
    second = _identity(database, "Noyra-storage-b")
    assert _STORAGE_KEY.fullmatch(first)
    assert _STORAGE_KEY.fullmatch(second)
    assert first != second
    assert first.casefold() != "noyra-storage-a"
    with database.transaction() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            "UPDATE subject_storage_keys SET storage_key = ? WHERE subject_id = ?",
            (second, "Noyra-storage-a"),
        )
    with database.transaction() as connection, pytest.raises(Exception, match="cannot be deleted"):
        connection.execute(
            "DELETE FROM subject_storage_keys WHERE subject_id = ?",
            ("Noyra-storage-a",),
        )


def test_legacy_workspace_is_atomically_migrated_to_keyed_directory(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-legacy-workspace"
    storage_key = _identity(database, subject_id)
    legacy = tmp_path / "workspace" / subject_id
    legacy.mkdir(parents=True)
    (legacy / "result.txt").write_text("legacy", encoding="utf-8")

    workspace = ProjectWorkspace(
        tmp_path / "workspace",
        storage_key,
        quota_bytes=1_000,
        legacy_subject_id=subject_id,
    )
    assert workspace.root == tmp_path / "workspace" / storage_key
    assert (workspace.root / "result.txt").read_text(encoding="utf-8") == "legacy"
    assert not legacy.exists()


def test_legacy_archive_queue_uses_keyed_directory(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-legacy-archive"
    storage_key = _identity(database, subject_id)
    legacy = tmp_path / "training_raw" / "archive_queue" / subject_id
    legacy.mkdir(parents=True)
    (legacy / "cold").mkdir()
    queue = ArchiveTransferQueue(database, subject_id, tmp_path / "training_raw")
    queue.enqueue("cold/state.bin", b"state")
    keyed = tmp_path / "training_raw" / "archive_queue" / storage_key
    assert (keyed / "cold" / "state.bin").read_bytes() == b"state"
    assert not legacy.exists()


def test_keyed_directory_reparse_point_is_rejected(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("portable symlink setup is unavailable on Windows CI")
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-reparse-subject"
    storage_key = _identity(database, subject_id)
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / storage_key).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProjectExecutionError, match="binding"):
        ProjectWorkspace(root, storage_key, quota_bytes=100, legacy_subject_id=subject_id)


def test_schema_39_subjects_receive_keys_during_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "noyra.sqlite3"
    database = Database(path)
    subject_id = "Noyra-schema-upgrade"
    _identity(database, subject_id)
    with database.transaction() as connection:
        connection.execute("DROP TABLE subject_storage_keys")
        connection.execute("UPDATE schema_meta SET value = '39' WHERE key = 'schema_version'")
    upgraded = Database(path)
    with upgraded.connection() as connection:
        row = connection.execute(
            "SELECT storage_key FROM subject_storage_keys WHERE subject_id = ?", (subject_id,)
        ).fetchone()
    assert row is not None and _STORAGE_KEY.fullmatch(str(row["storage_key"]))


def test_missing_mapping_fails_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-missing-storage"
    _identity(database, subject_id)
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_subject_storage_key_delete")
        connection.execute("DELETE FROM subject_storage_keys WHERE subject_id = ?", (subject_id,))
    with pytest.raises(IntegrityError, match="missing"):
        IdentityStore(database).storage_key(subject_id)
