from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from noyra.core import Database, EventStore
from noyra.core.errors import ArchiveUnavailableError, IntegrityError
from noyra.core.types import canonical_json
from noyra.migration.subject_payload import (
    INVENTORY_NAME,
    create_payload,
    restore_payload,
    verify_archives,
    verify_payload,
    verify_runtime_credentials,
)
from test_m42_p1_03_archive_integrity import _EVENT_ID, _SUBJECT_ID, _create_archived_fixture


def test_complete_payload_restores_real_cold_history_and_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    fixture = _create_archived_fixture(source, monkeypatch)
    workspace = source / "workspace" / "空目录"
    workspace.mkdir(parents=True)
    (workspace.parent / "工作.txt").write_text("保存的工作", encoding="utf-8")
    snapshot = tmp_path / "snapshot.sqlite3"
    with fixture.database.connection() as connection, sqlite3.connect(snapshot) as target:
        connection.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
    payload = tmp_path / "state.tar"
    create_payload(snapshot, source, payload, _SUBJECT_ID)
    target_root = tmp_path / "restored"
    database_path = restore_payload(payload, target_root, _SUBJECT_ID)
    verify_archives(target_root, _SUBJECT_ID)
    database = Database(database_path, initialize=False)
    events = EventStore(database)
    assert events.get(_EVENT_ID).payload == {"value": "original evidence"}
    assert (target_root / "workspace" / "空目录").is_dir()
    assert (target_root / "workspace" / "工作.txt").read_text(encoding="utf-8") == "保存的工作"
    # Removing an actual cold segment is rejected, both by inventory and by
    # archive-level restoration; a SQLite-only check cannot detect this loss.
    archived = target_root / fixture.archive_path.relative_to(source)
    archived.unlink()
    with pytest.raises(ValueError, match="file set mismatch"):
        verify_payload(target_root, _SUBJECT_ID)
    with pytest.raises((ArchiveUnavailableError, IntegrityError)):
        verify_archives(target_root, _SUBJECT_ID)


@pytest.mark.parametrize(
    "name",
    ["../escape", "workspace/../../escape", "/etc/shadow", "workspace/x:stream", "workspace/CON"],
)
def test_restore_rejects_inventory_path_escape(tmp_path: Path, name: str) -> None:
    inventory = {
        "format": "noyra-subject-state/v1",
        "subject_id": _SUBJECT_ID,
        "files": [{"path": name, "bytes": 0, "sha256": "0" * 64}],
        "directories": [],
        "total_bytes": 0,
    }
    raw = canonical_json(inventory).encode()
    path = tmp_path / "malformed.tar"
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo(INVENTORY_NAME)
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    with pytest.raises(ValueError):
        restore_payload(path, tmp_path / "restore", _SUBJECT_ID)
    assert not (tmp_path / "restore").exists()


def test_restored_inventory_hash_does_not_hide_changed_files(tmp_path: Path) -> None:
    database = Database(tmp_path / "source.sqlite3")
    payload = tmp_path / "payload.tar"
    create_payload(database.path, tmp_path, payload, _SUBJECT_ID)
    restored = tmp_path / "restored"
    restore_payload(payload, restored, _SUBJECT_ID)
    inventory = json.loads((restored / INVENTORY_NAME).read_text())
    assert len(inventory["files"]) == 1
    with (restored / "noyra.sqlite3").open("ab") as stream:
        stream.write(b"modified")
    with pytest.raises(ValueError, match="content mismatch"):
        verify_payload(restored, _SUBJECT_ID)


def test_preserved_config_requires_actual_target_credential(tmp_path: Path) -> None:
    import shutil

    from noyra.core import IdentityStore
    from noyra.research.provider import SearchProviderStore
    from noyra.research.types import SearchProviderInput

    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure(_SUBJECT_ID, "a" * 64)
    source_secrets = tmp_path / "source-secrets"
    SearchProviderStore(database, source_secrets).configure(
        _SUBJECT_ID,
        SearchProviderInput(provider_type="brave", label="migration", api_key="source-secret"),
        actor="operator",
    )
    target_secrets = tmp_path / "target-secrets"
    with pytest.raises(FileNotFoundError):
        verify_runtime_credentials(database.path, target_secrets, _SUBJECT_ID)
    shutil.copytree(source_secrets, target_secrets / "search")
    verify_runtime_credentials(database.path, target_secrets, _SUBJECT_ID)
    next((target_secrets / "search").glob("*.key")).write_text("wrong-secret")
    with pytest.raises(ValueError, match="credential is unavailable or mismatched"):
        verify_runtime_credentials(database.path, target_secrets, _SUBJECT_ID)
