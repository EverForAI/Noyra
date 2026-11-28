from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest

from noyra.core import EventStore, IdentityStore
from noyra.core import database as database_module
from noyra.core.at_rest import BackupKeyring, EncryptedBackupManager
from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash, new_id, utc_now
from noyra.mind import MemoryBlockStore, MemoryStore


def _source_memory(database: Database, subject_id: str, content: str) -> Any:
    event = EventStore(database).append(subject_id, "experience", "fixture", {"text": content})
    return MemoryStore(database).create(
        subject_id,
        "semantic",
        content,
        salience=0.6,
        confidence=0.8,
        source_event_ids=(event.event_id,),
    )


def test_memory_blocks_preserve_revisions_and_optimistic_concurrency() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "Noyra-memory-block-test"
        IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
        blocks = MemoryBlockStore(database)
        block = blocks.create(
            subject_id,
            "working_context",
            "current inquiry",
            "Compare two evidence sources.",
            reason="a bounded context became relevant",
            source_memory_ids=(),
            source_event_ids=(),
        )
        revised = blocks.revise(
            block.block_id,
            "Compare three evidence sources and record disagreement.",
            reason="new evidence broadened the inquiry",
            expected_version=1,
            source_memory_ids=(),
            source_event_ids=(),
        )
        assert revised.version == 2
        with pytest.raises(ValueError):
            blocks.revise(
                block.block_id,
                "stale revision",
                reason="stale writer",
                expected_version=1,
                source_memory_ids=(),
                source_event_ids=(),
            )
        assert blocks.verify_integrity(subject_id) == {
            "memory_blocks": 1,
            "memory_block_revisions": 2,
        }
        assert [item.lineage_status for item in blocks.lineage_history(block.block_id)] == [
            "declared_no_memory_sources",
            "declared_no_memory_sources",
        ]


def test_memory_block_sources_capture_exact_immutable_memory_revision() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "Noyra-memory-block-lineage-test"
        IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
        memories = MemoryStore(database)
        source = _source_memory(database, subject_id, "first source revision")
        blocks = MemoryBlockStore(database)
        block = blocks.create(
            subject_id,
            "knowledge_focus",
            "source-aware focus",
            "A context derived from the source memory.",
            reason="memory-derived context",
            source_memory_ids=(source.memory_id,),
            source_event_ids=(),
        )

        first = blocks.lineage_history(block.block_id)[0]
        assert first.lineage_status == "memory"
        assert len(first.sources) == 1
        assert first.sources[0].memory_id == source.memory_id
        assert first.sources[0].revision_number == 1
        assert len(first.sources[0].revision_hash) == 64

        second_event = EventStore(database).append(
            subject_id, "experience", "fixture", {"text": "second source revision"}
        )
        memories.revise(
            source.memory_id,
            content="second source revision",
            salience=0.7,
            confidence=0.9,
            status="active",
            reason="updated source evidence",
            source_event_ids=(second_event.event_id,),
            expected_revision=1,
        )
        # A later source edit must not silently rewrite the revision the block
        # actually cited.
        assert blocks.lineage_history(block.block_id)[0].sources == first.sources

        blocks.revise(
            block.block_id,
            "A later context derived from the revised source.",
            reason="new source revision became relevant",
            expected_version=1,
            source_memory_ids=(source.memory_id,),
            source_event_ids=(),
        )
        history = blocks.lineage_history(block.block_id)
        assert history[1].sources[0].revision_number == 2
        assert history[1].sources[0].revision_hash != first.sources[0].revision_hash
        assert blocks.verify_integrity(subject_id)["memory_block_revisions"] == 2


def test_memory_block_lineage_requires_explicit_same_subject_sources() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "Noyra-memory-block-lineage-owner"
        other_subject = "Noyra-memory-block-lineage-other"
        IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
        IdentityStore(database).ensure(other_subject, content_hash({"seed": other_subject}))
        blocks = MemoryBlockStore(database)
        with pytest.raises(ValueError, match="explicitly declared"):
            blocks.create(
                subject_id,
                "working_context",
                "implicit lineage",
                "content",
                reason="test",
            )
        with pytest.raises(ValueError, match="event lineage must be explicitly declared"):
            blocks.create(
                subject_id,
                "working_context",
                "implicit event lineage",
                "content",
                reason="test",
                source_memory_ids=(),
            )

        foreign = _source_memory(database, other_subject, "foreign source")
        foreign_event = EventStore(database).append(
            other_subject, "experience", "fixture", {"detail": "foreign"}
        )
        with pytest.raises(ValueError, match="source event is unavailable"):
            blocks.create(
                subject_id,
                "working_context",
                "foreign event lineage",
                "content",
                reason="test",
                source_memory_ids=(),
                source_event_ids=(foreign_event.event_id,),
            )
        with pytest.raises(ValueError, match="source memory is unavailable"):
            blocks.create(
                subject_id,
                "working_context",
                "foreign lineage",
                "content",
                reason="test",
                source_memory_ids=(foreign.memory_id,),
                source_event_ids=(),
            )
        with database.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM memory_blocks").fetchone()[0] == 0
            assert (
                connection.execute("SELECT COUNT(*) FROM memory_block_revisions").fetchone()[0] == 0
            )


def test_memory_block_direct_event_sources_are_chain_bound_and_complete(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-memory-block-event-lineage"
    IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
    event = EventStore(database).append(
        subject_id, "experience", "fixture", {"detail": "private-derived source"}
    )
    block = MemoryBlockStore(database).create(
        subject_id,
        "working_context",
        "event-linked",
        "derived directly from an event",
        reason="test",
        source_memory_ids=(),
        source_event_ids=(event.event_id,),
    )

    lineage = MemoryBlockStore(database).lineage_history(block.block_id)[0]
    assert lineage.lineage_status == "declared_no_memory_sources"
    assert lineage.event_lineage_complete is True
    assert len(lineage.event_sources) == 1
    assert lineage.event_sources[0].event_id == event.event_id
    assert lineage.event_sources[0].sequence_number > 0
    assert len(lineage.event_sources[0].root_hash) == 64
    assert MemoryBlockStore(database).verify_integrity(subject_id)["memory_block_revisions"] == 1

    with (
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
        database.transaction() as connection,
    ):
        connection.execute(
            "DELETE FROM memory_block_revision_event_sources "
            "WHERE revision_id=(SELECT revision_id FROM memory_block_revisions "
            "WHERE block_id=?)",
            (block.block_id,),
        )


def test_memory_block_source_rows_are_append_only(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-memory-block-append-only"
    IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
    source = _source_memory(database, subject_id, "source")
    block = MemoryBlockStore(database).create(
        subject_id,
        "working_context",
        "linked",
        "linked content",
        reason="test",
        source_memory_ids=(source.memory_id,),
        source_event_ids=(),
    )
    with database.connection() as connection:
        source_row = connection.execute(
            "SELECT revision_id,source_memory_id FROM memory_block_revision_sources"
        ).fetchone()
    with (
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
        database.transaction() as connection,
    ):
        connection.execute(
            "DELETE FROM memory_block_revision_sources WHERE revision_id=? AND source_memory_id=?",
            tuple(source_row),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
        database.transaction() as connection,
    ):
        connection.execute(
            "UPDATE memory_block_revision_sources SET source_revision_number=2 "
            "WHERE revision_id=? AND source_memory_id=?",
            tuple(source_row),
        )
    assert MemoryBlockStore(database).lineage_history(block.block_id)[0].sources[0].memory_id == (
        source.memory_id
    )

    independent = MemoryBlockStore(database).create(
        subject_id,
        "working_context",
        "declared no memory parent",
        "content",
        reason="test",
        source_memory_ids=(),
        source_event_ids=(),
    )
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_memory_block_revision_event_coverage_delete")
        connection.execute(
            "DELETE FROM memory_block_revision_event_coverage WHERE revision_id=("
            "SELECT revision_id FROM memory_block_revisions WHERE block_id=? AND version=1)",
            (independent.block_id,),
        )
        connection.execute("DROP TRIGGER prevent_memory_block_revision_lineage_delete")
        connection.execute(
            "DELETE FROM memory_block_revision_lineage WHERE revision_id=("
            "SELECT revision_id FROM memory_block_revisions WHERE block_id=? AND version=1)",
            (independent.block_id,),
        )
    with pytest.raises(IntegrityError, match="lineage record is missing"):
        MemoryBlockStore(database).verify_integrity(subject_id)


def test_memory_block_source_lineage_survives_encrypted_backup_restore(tmp_path: Path) -> None:
    current = tmp_path / "current"
    database = Database(current / "noyra.sqlite3")
    subject_id = "Noyra-memory-block-backup"
    IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
    source = _source_memory(database, subject_id, "backup source")
    block = MemoryBlockStore(database).create(
        subject_id,
        "working_context",
        "backup-linked",
        "backup context",
        reason="backup fixture",
        source_memory_ids=(source.memory_id,),
        source_event_ids=(),
    )
    expected = MemoryBlockStore(database).lineage_history(block.block_id)
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(current, keyring)
    backup = tmp_path / "lineage.noyra-backup"
    manager.create(backup)

    destination = tmp_path / "restored"
    manager.restore(backup, destination)
    restored = Database(
        destination / "noyra.sqlite3", initialize=False, strict_schema_contract=True
    )
    restored_blocks = MemoryBlockStore(restored)
    assert restored_blocks.lineage_history(block.block_id) == expected
    assert restored_blocks.verify_integrity(subject_id)["memory_block_revisions"] == 1


def test_schema_122_memory_lineage_remains_direct_event_incomplete_after_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "schema-122.sqlite3"
    subject_id = "Noyra-memory-block-v122-lineage"
    with monkeypatch.context() as historical:
        historical.setattr(database_module, "CURRENT_SCHEMA_VERSION", 122)
        database = Database(path)
        IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
        source = _source_memory(database, subject_id, "v122 memory source")
        block_id = new_id("mblock")
        revision_id = new_id("mblockrev")
        content = "v122 memory-derived block"
        now = utc_now()
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO memory_blocks(block_id,subject_id,block_type,label,content,"
                "content_hash,privacy_level,status,version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'active',1,?,?)",
                (
                    block_id,
                    subject_id,
                    "working_context",
                    "v122",
                    content,
                    content_hash(content),
                    "private",
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO memory_block_revisions(revision_id,block_id,version,content,"
                "content_hash,status,reason,actor,created_at) VALUES(?,?,1,?,?,'active',?,?,?)",
                (
                    revision_id,
                    block_id,
                    content,
                    content_hash(content),
                    "v122 fixture",
                    "subject",
                    now,
                ),
            )
            sources = MemoryBlockStore._capture_sources(connection, subject_id, (source.memory_id,))
            lineage_hash = MemoryBlockStore._lineage_hash("memory", sources)
            connection.execute(
                "INSERT INTO memory_block_revision_lineage(revision_id,block_id,lineage_status,"
                "lineage_hash) VALUES(?,?,'memory',?)",
                (revision_id, block_id, lineage_hash),
            )
            for item in sources:
                connection.execute(
                    "INSERT INTO memory_block_revision_sources(revision_id,block_id,"
                    "source_memory_id,source_revision_number,source_revision_hash) "
                    "VALUES(?,?,?,?,?)",
                    (
                        revision_id,
                        block_id,
                        item.memory_id,
                        item.revision_number,
                        item.revision_hash,
                    ),
                )

    upgraded = Database(path, strict_schema_contract=True)
    lineage = MemoryBlockStore(upgraded).lineage_history(block_id)[0]
    assert lineage.lineage_status == "memory"
    assert lineage.sources[0].memory_id == source.memory_id
    # v122 recorded memory parents but had no explicit direct-event coverage.
    assert lineage.event_lineage_complete is False
    assert lineage.event_sources == ()


def test_schema_121_memory_block_revisions_migrate_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.sqlite3"
    subject_id = "Noyra-legacy-memory-block-lineage"
    with monkeypatch.context() as historical:
        historical.setattr(database_module, "CURRENT_SCHEMA_VERSION", 121)
        database = Database(path)
        IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
        block_id = new_id("mblock")
        revision_id = new_id("mblockrev")
        content = "legacy block with unknown provenance"
        now = utc_now()
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO memory_blocks(block_id,subject_id,block_type,label,content,"
                "content_hash,privacy_level,status,version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'active',1,?,?)",
                (
                    block_id,
                    subject_id,
                    "working_context",
                    "legacy",
                    content,
                    content_hash(content),
                    "private",
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO memory_block_revisions(revision_id,block_id,version,content,"
                "content_hash,status,reason,actor,created_at) VALUES(?,?,1,?,?,'active',?,?,?)",
                (
                    revision_id,
                    block_id,
                    content,
                    content_hash(content),
                    "legacy entry",
                    "subject",
                    now,
                ),
            )

    upgraded = Database(path)
    lineage = MemoryBlockStore(upgraded).lineage_history(block_id)
    assert len(lineage) == 1
    assert lineage[0].lineage_status == "unknown"
    assert lineage[0].sources == ()
    assert MemoryBlockStore(upgraded).verify_integrity(subject_id)["memory_block_revisions"] == 1
    with upgraded.connection() as connection:
        version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
        )
        assert version == database_module.CURRENT_SCHEMA_VERSION
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
