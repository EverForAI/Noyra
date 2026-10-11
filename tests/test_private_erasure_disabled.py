from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.at_rest import BackupKeyring, EncryptedBackupManager
from noyra.core.export_jobs import assert_no_private_erasure, private_erasure_epoch
from noyra.core.types import content_hash
from noyra.interaction import InteractionStore
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.erasure import ConversationErasure, ErasureReviewRequired
from noyra.mind import MemoryStore
from noyra.service import NoyraService, ServiceSettings


def test_disabled_new_intents_and_empty_recovery_preserve_entire_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("owner", content_hash("owner"))
    window = ConversationStore(database).create("owner", "keep this conversation")
    message = InteractionStore(database).receive(
        "owner", "web", "web-user", "keep the original", conversation_id=window["conversation_id"]
    )
    erasure = ConversationErasure(database)
    with database.read_transaction() as connection:
        before = list(connection.iterdump())
    attempts = (("owner", 1, True), ("owner", 0, False), ("other", 1, True))
    for subject, revision, confirmed in attempts:
        with pytest.raises(ErasureReviewRequired, match=r"^private_deletion_disabled$"):
            erasure.request(
                subject, window["conversation_id"], expected_revision=revision, confirmed=confirmed
            )
    assert erasure.resume("owner") == []
    assert erasure.resume("other") == []
    with database.read_transaction() as connection:
        assert list(connection.iterdump()) == before
        assert private_erasure_epoch(connection, "owner") == 0
        assert_no_private_erasure(connection, "owner", epoch=0)
    assert not erasure.root.exists()
    assert InteractionStore(database).get(message.interaction_id).content == "keep the original"


def test_compatibility_recovery_cannot_create_an_intent_without_a_marker(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("owner", content_hash("owner"))
    window = ConversationStore(database).create("owner", "keep")
    erasure = ConversationErasure(database)
    with pytest.raises(FileNotFoundError):
        erasure._resume_recorded_intent("owner", window["conversation_id"], expected_revision=1)
    assert erasure.get("owner", window["conversation_id"]) is None
    assert not erasure.root.exists()
    assert ConversationStore(database).list("owner")["items"][0]["status"] == "active"


def test_service_boot_and_restore_without_intents_keep_chat_and_memory(tmp_path: Path) -> None:
    data = tmp_path / "data"
    subject = "recovery-owner"
    settings = ServiceSettings(
        data_dir=data,
        subject_id=subject,
        genesis_hash=content_hash(subject),
        host="127.0.0.1",
        port=0,
    )
    service = NoyraService(settings)
    database = service.kernel.database
    try:
        window = ConversationStore(database).create(subject, "keep this window")
        message = InteractionStore(database).receive(
            subject, "web", "web-user", "keep this text", conversation_id=window["conversation_id"]
        )
        with database.connection() as connection:
            event = connection.execute(
                "SELECT event_id FROM events WHERE subject_id=? AND event_type=? "
                "AND json_extract(payload_json,'$.interaction_id')=?",
                (subject, "interaction_received", message.interaction_id),
            ).fetchone()[0]
        memory = MemoryStore(database).create(
            subject,
            "episodic",
            "keep this fact",
            salience=0.5,
            confidence=0.7,
            source_event_ids=(event,),
        )
        revisions = MemoryStore(database).revisions(memory.memory_id)
        service.boot()
        assert InteractionStore(database).get(message.interaction_id).content == "keep this text"
        assert MemoryStore(database).revisions(memory.memory_id) == revisions
        assert ConversationStore(database).list(subject)["items"][0]["status"] == "active"
        assert not (data / "privacy-deletions").exists()
    finally:
        service.close()

    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    manager = EncryptedBackupManager(data, keyring)
    backup = tmp_path / "unchanged.noyra-backup"
    manager.create(backup)
    restored_path = manager.restore(backup, tmp_path / "restored") / "noyra.sqlite3"
    with database.read_transaction() as original, closing(sqlite3.connect(restored_path)) as copy:
        assert list(copy.iterdump()) == list(original.iterdump())
    restored = Database(restored_path)
    assert InteractionStore(restored).get(message.interaction_id).content == "keep this text"
    assert MemoryStore(restored).revisions(memory.memory_id) == revisions
    assert not (tmp_path / "restored" / "privacy-deletions").exists()
