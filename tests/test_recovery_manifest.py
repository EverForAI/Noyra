from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from noyra.core.recovery_manifest import build_recovery_manifest, write_recovery_manifest


def test_recovery_manifest_contains_references_but_no_secret_material(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    credentials_dir = tmp_path / "credentials"
    data_dir.mkdir()
    backup_dir.mkdir()
    credentials_dir.mkdir()
    database = data_dir / "noyra.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '96')")
    connection.commit()
    connection.close()
    backup = backup_dir / "noyra-verified.noyra-backup"
    backup.write_bytes(b"encrypted backup")

    manifest = build_recovery_manifest(
        release_id="release-1",
        source_sha="a" * 40,
        data_dir=data_dir,
        backup_dir=backup_dir,
        backup_path=backup,
        backup_keyring_path=tmp_path / "backup-keyring.json",
        credentials_dir=credentials_dir,
        server_identity_path=tmp_path / "identity.json",
        database_path=database,
        generated_at="2026-10-17T00:00:00+00:00",
    )
    target = tmp_path / "recovery-manifest.json"
    write_recovery_manifest(target, manifest)

    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded["format"] == "noyra-recovery-manifest/v1"
    assert loaded["release"]["schema_version"] == 96
    assert loaded["paths"]["latest_backup_or_directory"]["path"] == str(backup)
    serialized = target.read_text(encoding="utf-8")
    assert "encrypted backup" not in serialized
    assert "password" not in serialized.lower()
    assert "token" not in serialized.lower()
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_recovery_manifest_rejects_symlink_destination(tmp_path: Path) -> None:
    target = tmp_path / "recovery-manifest.json"
    destination = tmp_path / "destination.json"
    destination.write_text("keep", encoding="utf-8")
    try:
        target.symlink_to(destination)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlinks unavailable: {error}")
    with pytest.raises(ValueError, match="regular file"):
        write_recovery_manifest(target, {"format": "test"})
