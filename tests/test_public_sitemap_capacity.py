from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from noyra.core.database import CURRENT_SCHEMA_VERSION, Database
from noyra.core.errors import IntegrityError
from noyra.core.identity import IdentityStore
from noyra.core.types import content_hash
from noyra.interaction.diary import PublicDiaryStore
from noyra.interaction.public_archive import PublicArchive


def test_slots_grow_without_renumbering_and_replay_is_idempotent(tmp_path: Path) -> None:
    from noyra.core.public_sitemap_schema import MIGRATION_V94
    from noyra.interaction.sitemap_tokens import SitemapTokens

    database = Database(tmp_path / "growth.sqlite3")
    subject = "sitemap-growth"
    IdentityStore(database).ensure(subject, content_hash(subject))
    store = PublicArchive(database, sitemap_tokens=SitemapTokens((b"R" * 32,)))
    for index in range(101):
        store.publish(
            subject,
            {
                "publication_id": f"item-{index:05}",
                "kind": "goal",
                "title": "Approved",
                "summary": "Public",
                "body": "Public",
                "details": {},
                "expected_revision": 0,
                "confirm_public": True,
            },
            actor="test",
        )
    tokens = store.sitemap_partitions(subject)
    assert len(tokens) == 2
    store.withdraw(subject, "item-00000", 1, actor="test")
    with database.transaction() as connection:
        before = connection.execute("SELECT * FROM public_sitemap_slots ORDER BY slot").fetchall()
        Database._execute_sql_script(connection, MIGRATION_V94)
        assert [tuple(row) for row in before] == [
            tuple(row)
            for row in connection.execute("SELECT * FROM public_sitemap_slots ORDER BY slot")
        ]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM public_sitemap_slots")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE public_sitemap_slots SET item_id='other'")
    assert store.sitemap_partitions(subject) == tokens
    assert "item-00000" not in {row["id"] for row in store.sitemap_partition(subject, tokens[0])}


def test_index_25000_valid_diaries_is_bounded_stable_and_shards_verify(tmp_path: Path) -> None:
    database = Database(tmp_path / "capacity.sqlite3")
    subject = "sitemap-capacity-subject"
    IdentityStore(database).ensure(subject, content_hash(subject))
    with database.transaction() as connection:
        for index in range(25_000):
            row = {
                "entry_id": f"diary-{index:06}",
                "subject_id": subject,
                "source_sleep_id": None,
                "title": "Approved diary",
                "body": "Public body",
                "body_hash": content_hash("Public body"),
                "idempotency_key": str(index),
                "created_at": f"2020-01-{index % 28 + 1:02}T00:00:{index % 60:02}.000+00:00",
            }
            row["state_hash"] = content_hash(
                {
                    "source_sleep_id": None,
                    "title": "Approved diary",
                    "body_hash": content_hash("Public body"),
                }
            )
            connection.execute(
                f"INSERT INTO public_diary_entries({','.join(row)}) "
                f"VALUES({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
    store = PublicArchive(database)
    tokens = store.sitemap_partitions(subject)
    assert len(tokens) == 250
    assert store.sitemap_partitions(subject) == tokens
    ids = [item["id"] for token in tokens for item in store.sitemap_partition(subject, token)]
    assert len(ids) == len(set(ids)) == 25_000
    assert store.sitemap_partitions("unrelated-subject") == []
    # Do not bypass immutable source triggers to manufacture the failure.
    with (
        patch.object(PublicDiaryStore, "_from_row", side_effect=IntegrityError("hash mismatch")),
        pytest.raises(IntegrityError),
    ):
        store.sitemap_partition(subject, tokens[0])


def test_v93_backfill_is_subject_scoped_and_never_publishes_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import noyra.core.database as schema
    from noyra.interaction.store import InteractionStore

    with monkeypatch.context() as old:
        old.setattr(schema, "CURRENT_SCHEMA_VERSION", 93)
        database = Database(tmp_path / "v93.sqlite3")
        for subject in ("sitemap-migrate-one", "sitemap-migrate-two"):
            IdentityStore(database).ensure(subject, content_hash(subject))
            InteractionStore(database).send(subject, "private:local", "peer", "PRIVATE")
            InteractionStore(database).send(subject, "public:local", "peer", "Approved")
    upgraded = Database(database.path)
    with upgraded.connection() as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(CURRENT_SCHEMA_VERSION)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM public_sitemap_slots").fetchone()[0] == 2
    store = PublicArchive(upgraded)
    for subject in ("sitemap-migrate-one", "sitemap-migrate-two"):
        items = [
            item
            for token in store.sitemap_partitions(subject)
            for item in store.sitemap_partition(subject, token)
        ]
        assert len(items) == 1 and items[0]["kind"] == "interaction"
        assert "PRIVATE" not in str(items)
