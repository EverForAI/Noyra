from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from noyra.core.archive import ArchiveKeyMaterial, ArchiveKeyring
from noyra.core.at_rest import AtRestError
from noyra.core.database import Database
from noyra.core.errors import ArchiveKeyUnavailableError, IntegrityError
from noyra.core.identity import IdentityStore
from noyra.core.integrity import IntegrityRegistry
from noyra.core.types import content_hash, utc_now

_KEY_1 = b"1" * 32
_KEY_2 = b"2" * 32
_KEY_3 = b"3" * 32


def _material(key_id: str, key: bytes, status: str) -> ArchiveKeyMaterial:
    return ArchiveKeyMaterial(key_id, key, hashlib.sha256(key).hexdigest(), status)


def _ring(
    generation: int,
    active_key_id: str,
    *materials: ArchiveKeyMaterial,
    legacy_key_id: str | None = None,
) -> ArchiveKeyring:
    return ArchiveKeyring(
        generation=generation,
        active_key_id=active_key_id,
        legacy_key_id=legacy_key_id,
        keys=materials,
    )


def _subject(database: Database, subject_id: str) -> None:
    IdentityStore(database).ensure(subject_id, content_hash({"subject_id": subject_id}))


def test_new_and_dormant_subjects_can_catch_up_to_current_global_generation(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    dormant = "Noyra-gate1-dormant"
    new_subject = "Noyra-gate1-new"
    active_subject = "Noyra-gate1-active"
    _subject(database, dormant)
    _subject(database, new_subject)
    _subject(database, active_subject)

    old = _ring(3, "key-1", _material("key-1", _KEY_1, "active"), legacy_key_id="key-1")
    assert old.record_revision(database, dormant)
    for generation in range(4, 9):
        assert _ring(
            generation,
            "key-1",
            _material("key-1", _KEY_1, "active"),
            legacy_key_id="key-1",
        ).record_revision(database, active_subject)
    current = _ring(
        9,
        "key-2",
        _material("key-1", _KEY_1, "retired"),
        _material("key-2", _KEY_2, "active"),
        legacy_key_id="key-1",
    )

    assert current.record_revision(database, active_subject)
    assert current.record_revision(database, dormant)
    assert current.record_revision(database, new_subject)
    assert not current.record_revision(database, new_subject)
    with database.connection() as connection:
        dormant_generations = [
            int(row[0])
            for row in connection.execute(
                "SELECT generation FROM archive_keyring_revisions WHERE subject_id = ? "
                "ORDER BY generation",
                (dormant,),
            )
        ]
        new_generations = [
            int(row[0])
            for row in connection.execute(
                "SELECT generation FROM archive_keyring_revisions WHERE subject_id = ?",
                (new_subject,),
            )
        ]
    assert dormant_generations == [3, 9]
    assert new_generations == [9]


def test_established_global_ledger_rejects_unrecorded_generation_gap(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-gate1-global-gap"
    _subject(database, subject_id)
    first = _ring(4, "key-1", _material("key-1", _KEY_1, "active"))
    assert first.record_revision(database, subject_id)
    skipped = _ring(7, "key-1", _material("key-1", _KEY_1, "active"))
    with pytest.raises(ArchiveKeyUnavailableError, match="global generation is not sequential"):
        skipped.record_revision(database, subject_id)


def test_database_trigger_rejects_catch_up_to_stale_global_generation(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    active = "Noyra-gate1-current-generation"
    dormant = "Noyra-gate1-stale-catch-up"
    _subject(database, active)
    _subject(database, dormant)
    generation_four = _ring(4, "key-1", _material("key-1", _KEY_1, "active"))
    generation_five = _ring(5, "key-1", _material("key-1", _KEY_1, "active"))
    assert generation_four.record_revision(database, active)
    assert generation_five.record_revision(database, active)

    with database.connection() as connection:
        row = connection.execute(
            "SELECT * FROM archive_keyring_revisions WHERE subject_id = ? AND generation = 4",
            (active,),
        ).fetchone()
    with (
        pytest.raises(sqlite3.IntegrityError, match="generation must advance"),
        database.transaction() as connection,
    ):
        connection.execute(
            "INSERT INTO archive_keyring_revisions(revision_id, subject_id, generation, "
            "format, active_key_id, legacy_key_id, key_metadata_json, metadata_hash, "
            "state_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "archive-keyring-rev-stale-catch-up",
                dormant,
                row["generation"],
                row["format"],
                row["active_key_id"],
                row["legacy_key_id"],
                row["key_metadata_json"],
                row["metadata_hash"],
                row["state_hash"],
                utc_now(),
            ),
        )


def test_global_generation_rollback_and_same_generation_conflict_fail_closed(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    first_subject = "Noyra-gate1-global-a"
    second_subject = "Noyra-gate1-global-b"
    _subject(database, first_subject)
    _subject(database, second_subject)
    current = _ring(8, "key-1", _material("key-1", _KEY_1, "active"))
    assert current.record_revision(database, first_subject)

    rollback = _ring(7, "key-1", _material("key-1", _KEY_1, "active"))
    with pytest.raises(ArchiveKeyUnavailableError, match="rolled back"):
        rollback.record_revision(database, second_subject)

    conflict = _ring(8, "key-2", _material("key-2", _KEY_2, "active"))
    with pytest.raises(ArchiveKeyUnavailableError, match="different global"):
        conflict.record_revision(database, second_subject)


def test_key_id_fingerprint_binding_is_global_across_subjects(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    first_subject = "Noyra-gate1-binding-a"
    second_subject = "Noyra-gate1-binding-b"
    _subject(database, first_subject)
    _subject(database, second_subject)
    first = _ring(2, "stable", _material("stable", _KEY_1, "active"))
    assert first.record_revision(database, first_subject)
    reused = _ring(3, "stable", _material("stable", _KEY_2, "active"))

    with pytest.raises(ArchiveKeyUnavailableError, match="cannot be reused"):
        reused.record_revision(database, second_subject)


def test_segment_fingerprint_conflict_is_integrity_failure_not_silently_skipped(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-gate1-segment-binding"
    _subject(database, subject_id)
    keyring = _ring(4, "key-1", _material("key-1", _KEY_1, "active"))
    assert keyring.record_revision(database, subject_id)
    now = utc_now()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO event_payload_segments(segment_id, subject_id, object_key, "
            "first_occurred_at, last_occurred_at, event_count, compressed_hash, "
            "archive_format, encryption_key_id, encryption_key_fingerprint, created_at) "
            "VALUES ('segment-1', ?, 'events/segment-1', ?, ?, 1, ?, 'v2', 'key-1', ?, ?)",
            (subject_id, now, now, "a" * 64, "f" * 64, now),
        )

    with pytest.raises(IntegrityError, match="fingerprint conflicts"):
        keyring.validate_revision(database, subject_id)


def test_corrupt_revision_metadata_and_hashes_fail_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-gate1-revision-corrupt"
    _subject(database, subject_id)
    keyring = _ring(6, "key-1", _material("key-1", _KEY_1, "active"))
    assert keyring.record_revision(database, subject_id)
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_archive_keyring_revision_update")
        connection.execute(
            "UPDATE archive_keyring_revisions SET key_metadata_json = '{}' WHERE subject_id = ?",
            (subject_id,),
        )

    with pytest.raises(IntegrityError, match="revision metadata is invalid"):
        keyring.validate_revision(database, subject_id)


@pytest.mark.parametrize("field", ["metadata_hash", "state_hash"])
def test_corrupt_revision_hashes_fail_closed(tmp_path: Path, field: str) -> None:
    database = Database(tmp_path / f"{field}.sqlite3")
    subject_id = f"Noyra-gate1-{field.replace('_', '-')}"
    _subject(database, subject_id)
    keyring = _ring(6, "key-3", _material("key-3", _KEY_3, "active"))
    assert keyring.record_revision(database, subject_id)
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_archive_keyring_revision_update")
        connection.execute(
            f"UPDATE archive_keyring_revisions SET {field} = ? WHERE subject_id = ?",
            ("0" * 64, subject_id),
        )

    with pytest.raises(IntegrityError, match="revision hash is invalid"):
        keyring.validate_revision(database, subject_id)


def test_archive_keyring_path_fails_closed_when_acl_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "archive-keyring.json"
    path.write_text("{}", encoding="utf-8")

    def reject(_: Path | str) -> Path:
        raise AtRestError("unsafe ACL")

    monkeypatch.setattr("noyra.core.archive.validate_keyring_path", reject)
    with pytest.raises(ArchiveKeyUnavailableError, match="path or ACL is unsafe"):
        ArchiveKeyring.from_path(path)


def test_integrity_accepts_subject_catch_up_revision_history(tmp_path: Path) -> None:
    """A dormant subject may jump to a later global generation."""
    database = Database(tmp_path / "noyra.sqlite3")
    dormant = "Noyra-gate1-integrity-dormant"
    active = "Noyra-gate1-integrity-active"
    _subject(database, dormant)
    _subject(database, active)

    first = _ring(3, "key-1", _material("key-1", _KEY_1, "active"))
    assert first.record_revision(database, dormant)
    for generation in range(4, 9):
        assert _ring(
            generation,
            "key-1",
            _material("key-1", _KEY_1, "active"),
        ).record_revision(database, active)
    current = _ring(
        9,
        "key-2",
        _material("key-1", _KEY_1, "retired"),
        _material("key-2", _KEY_2, "active"),
    )
    assert current.record_revision(database, active)
    assert current.record_revision(database, dormant)

    report = IntegrityRegistry().run(
        database,
        dormant,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=15,
        check_ids=("core.archive_dead_letter",),
    )
    result = report.checks[0]
    assert result.status == "ok"
    assert result.details["keyring_revisions"] == 2
    assert result.details["key_metadata_entries"] == 3
