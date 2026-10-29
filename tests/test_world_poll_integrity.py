from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import Database, IdentityStore, IntegrityRegistry
from noyra.core.database import CURRENT_SCHEMA_VERSION
from noyra.core.errors import IntegrityError, SchemaContractError
from noyra.core.integrity import IntegrityAuditLimits
from noyra.core.schema.contracts import (
    _SCHEMA_DDL_FINGERPRINTS,
    _SCHEMA_STRUCTURE_FINGERPRINTS,
)
from noyra.core.types import content_hash
from noyra.world import FetchedDocument, ObservationStore, SourceRegistry, WorldIntegrity
from noyra.world.feed import FEED_FINGERPRINT_VERSION, feed_content, parse_feed
from noyra.world.poll import SourcePollStore
from noyra.world.types import SourceRecord

SUBJECT = "poll-integrity-test"
T1 = "2026-08-11T00:00:00.000+00:00"
T2 = "2026-08-11T00:15:00.000+00:00"
T3 = "2026-08-11T00:30:00.000+00:00"


@pytest.fixture
def context(tmp_path: Path) -> tuple[Database, SourceRecord]:
    database = Database(tmp_path / "test.sqlite3")
    IdentityStore(database).ensure(SUBJECT, content_hash("poll-integrity-seed"))
    source = SourceRegistry(database).register(
        SUBJECT, "News", "https://example.com/rss", "rss", status="active"
    )
    return database, source


def ingest(database: Database, source: SourceRecord, *ids: str, at: str = T1) -> None:
    xml = (
        "<rss><channel><title>News</title>"
        + "".join(
            f"<item><guid>{identifier}</guid><title>Update {identifier}</title>"
            f"<link>https://example.com/{identifier}</link></item>"
            for identifier in ids
        )
        + "</channel></rss>"
    )
    title, items = parse_feed(xml, source.url)
    content = feed_content(items)
    document = FetchedDocument(
        source.url,
        title,
        content,
        content_hash(content),
        "application/rss+xml",
        (),
        None,
        None,
        at,
        items,
    )
    ObservationStore(database).ingest(source.subject_id, source.source_id, document)


def update_poll(database: Database, **changes: Any) -> None:
    with database.transaction() as connection:
        row = connection.execute("SELECT * FROM world_poll_state").fetchone()
        assert row is not None
        payload = dict(row)
        payload.pop("state_hash")
        payload.update(changes)
        connection.execute(
            "UPDATE world_poll_state SET "
            + ",".join(f"{name}=?" for name in changes)
            + ",state_hash=? WHERE source_id=?",
            (*changes.values(), content_hash(payload), row["source_id"]),
        )


def corrupt_seen(database: Database, column: str, value: object) -> None:
    # Simulate pre-existing/offline corruption. Ordinary SQL updates must be
    # rejected by v109; row validation must still catch a damaged legacy image.
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_world_feed_seen_update")
        connection.execute(f"UPDATE world_feed_seen SET {column}=?", (value,))


def test_empty_acquisition_state_is_compatible(context: tuple[Database, SourceRecord]) -> None:
    database, _ = context
    result = WorldIntegrity(database).verify(SUBJECT)
    assert result["world_poll_state"] == 0
    assert result["world_feed_seen"] == 0


def test_changed_unchanged_failed_and_restart_are_read_only(
    context: tuple[Database, SourceRecord],
) -> None:
    database, source = context
    ingest(database, source, "one", "two")
    ingest(database, source, "two", "one", at=T2)
    SourcePollStore(database).failed(SUBJECT, source.source_id, T3)
    reopened = Database(database.path)
    with reopened.connection() as connection:
        before = "\n".join(connection.iterdump())
    counts = WorldIntegrity(reopened).verify(SUBJECT)
    with reopened.connection() as connection:
        after = "\n".join(connection.iterdump())
    assert before == after
    assert counts["world_poll_state"] == 1
    assert counts["world_feed_seen"] == 2


@pytest.mark.parametrize("kind", ["empty_feed", "first_failure", "web", "legacy"])
def test_valid_poll_baselines(context: tuple[Database, SourceRecord], kind: str) -> None:
    database, source = context
    if kind == "empty_feed":
        ingest(database, source)
    elif kind == "first_failure":
        SourcePollStore(database).failed(SUBJECT, source.source_id, T1)
    else:
        content = "Ordinary non-feed document"
        document = FetchedDocument(
            source.url,
            "News",
            content,
            content_hash(content),
            "text/html",
            (),
            None,
            None,
            T1,
        )
        store = ObservationStore(database)
        if kind == "legacy":
            store.record(SUBJECT, source.source_id, document)
            ingest(database, source, "one", at=T2)
        else:
            store.ingest(SUBJECT, source.source_id, document)
    counts = WorldIntegrity(database).verify(SUBJECT)
    assert counts["world_poll_state"] == 1
    assert counts["world_feed_seen"] == (1 if kind == "legacy" else 0)


def test_cache_pruning_does_not_require_all_lifetime_versions(
    context: tuple[Database, SourceRecord],
) -> None:
    database, source = context
    with patch("noyra.world.poll.MAX_SEEN_VERSIONS", 2):
        ingest(database, source, "one", "two")
        ingest(database, source, "three", at=T2)
    counts = WorldIntegrity(database).verify(SUBJECT)
    assert counts["world_feed_seen"] == 2
    with database.connection() as connection:
        assert (
            connection.execute("SELECT new_item_versions FROM world_poll_state").fetchone()[0] == 3
        )


def test_timezone_offsets_are_compared_as_instants(
    context: tuple[Database, SourceRecord],
) -> None:
    database, source = context
    ingest(database, source, "one")
    ingest(database, source, "one", at="2026-08-10T20:15:00.000-04:00")
    WorldIntegrity(database).verify(SUBJECT)


def test_poll_hash_tampering_is_detected(context: tuple[Database, SourceRecord]) -> None:
    database, source = context
    ingest(database, source, "one")
    with database.transaction() as connection:
        connection.execute("UPDATE world_poll_state SET checks=500")
    with pytest.raises(IntegrityError, match=r"world poll state.*hash mismatch"):
        WorldIntegrity(database).verify(SUBJECT)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("fingerprint_version", FEED_FINGERPRINT_VERSION + 1),
        ("checks", 1.5),
        ("unchanged_checks", 2),
        ("unchanged_checks", 1),
        ("new_item_versions", 0),
        ("last_attempt_at", "not-a-time"),
        ("last_attempt_at", "2026-08-11T00:00:00"),
        ("last_success_at", "2026-08-11T00:15:00+00:00"),
        ("last_success_at", None),
        ("last_change_at", "2026-08-11T00:15:00+00:00"),
        ("last_change_at", None),
    ],
)
def test_semantically_invalid_poll_with_recomputed_hash_is_detected(
    context: tuple[Database, SourceRecord],
    column: str,
    value: Any,
) -> None:
    database, source = context
    ingest(database, source, "one")
    update_poll(database, **{column: value})
    with pytest.raises(IntegrityError, match=r"world (poll state|feed seen)"):
        WorldIntegrity(database).verify(SUBJECT)


def test_non_json_sqlite_scalar_fails_as_integrity_error(
    context: tuple[Database, SourceRecord],
) -> None:
    database, source = context
    ingest(database, source, "one")
    with database.transaction() as connection:
        connection.execute("UPDATE world_poll_state SET last_attempt_at=?", (b"not-text",))
    with pytest.raises(IntegrityError, match=r"world poll state.*payload is invalid"):
        WorldIntegrity(database).verify(SUBJECT)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("item_key", "not-a-digest"),
        ("item_key", "A" * 64),
        ("fingerprint", "tampered"),
        ("fingerprint", b"a" * 64),
        ("fingerprint_version", FEED_FINGERPRINT_VERSION + 1),
        ("first_seen_at", "not-a-time"),
        ("first_seen_at", "2026-08-11T00:00:00"),
        ("first_seen_at", T2),
    ],
)
def test_damaged_legacy_seen_rows_are_detected(
    context: tuple[Database, SourceRecord],
    column: str,
    value: object,
) -> None:
    database, source = context
    ingest(database, source, "one")
    corrupt_seen(database, column, value)
    with pytest.raises(IntegrityError, match="world feed seen"):
        WorldIntegrity(database).verify(SUBJECT)


@pytest.mark.parametrize("table", ["world_poll_state", "world_feed_seen"])
def test_cross_subject_binding_is_checked_in_both_directions(
    context: tuple[Database, SourceRecord],
    table: str,
) -> None:
    database, source = context
    ingest(database, source, "one")
    other = "other-poll-subject"
    IdentityStore(database).ensure(other, content_hash(other))
    with database.transaction() as connection:
        trigger = (
            "validate_world_poll_subject_update"
            if table == "world_poll_state"
            else "prevent_world_feed_seen_update"
        )
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(f"UPDATE {table} SET subject_id=?", (other,))
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    for subject in (SUBJECT, other):
        with pytest.raises(IntegrityError):
            WorldIntegrity(database).verify(subject)


@pytest.mark.parametrize("table", ["world_poll_state", "world_feed_seen"])
def test_missing_acquisition_rows_fail_closed(
    context: tuple[Database, SourceRecord],
    table: str,
) -> None:
    database, source = context
    ingest(database, source, "one")
    with database.transaction() as connection:
        connection.execute(f"DELETE FROM {table}")
    with pytest.raises(IntegrityError, match="world feed seen"):
        WorldIntegrity(database).verify(SUBJECT)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("source_id", "other-source"),
        ("subject_id", "other-subject"),
        ("item_key", "0" * 64),
        ("fingerprint", "0" * 64),
        ("fingerprint_version", 2),
        ("first_seen_at", T2),
    ],
)
def test_seen_updates_are_rejected_before_business_state_changes(
    context: tuple[Database, SourceRecord],
    column: str,
    value: object,
) -> None:
    database, source = context
    ingest(database, source, "one")
    with (
        pytest.raises(sqlite3.IntegrityError, match="world feed seen entries are immutable"),
        database.transaction() as connection,
    ):
        connection.execute(f"UPDATE world_feed_seen SET {column}=?", (value,))
    assert WorldIntegrity(database).verify(SUBJECT)["world_feed_seen"] == 1


def test_world_registry_reports_corruption_not_ok(
    context: tuple[Database, SourceRecord],
    tmp_path: Path,
) -> None:
    database, source = context
    ingest(database, source, "one")
    with database.transaction() as connection:
        connection.execute("UPDATE world_poll_state SET state_hash=?", ("0" * 64,))
    report = IntegrityRegistry().run(
        database,
        SUBJECT,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=15,
        check_ids=("world.state",),
    )
    assert report.status == "corrupt"
    assert report.p0 == ("world.state:integrity_error",)
    assert report.checks[0].version == 2


def test_seen_scan_is_subject_to_existing_registry_budget(
    context: tuple[Database, SourceRecord],
    tmp_path: Path,
) -> None:
    database, source = context
    ingest(database, source, *(str(i) for i in range(8)))
    report = IntegrityRegistry().run(
        database,
        SUBJECT,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=15,
        check_ids=("world.state",),
        limits=IntegrityAuditLimits(max_rows_per_check=4),
    )
    assert report.status == "degraded"
    assert report.p0 == ()
    assert report.p1 == ("world.state:row_limit",)


@pytest.mark.parametrize("corruption", [None, "poll", "seen"])
def test_v108_upgrade_is_additive_and_preserves_real_state(
    tmp_path: Path,
    corruption: str | None,
) -> None:
    with patch("noyra.core.database.CURRENT_SCHEMA_VERSION", 108):
        old = Database(tmp_path / "v108.sqlite3", strict_schema_contract=True)
        IdentityStore(old).ensure(SUBJECT, content_hash("old-poll-seed"))
        source = SourceRegistry(old).register(
            SUBJECT, "News", "https://example.com/rss", "rss", status="active"
        )
        ingest(old, source, "one")
        with old.transaction() as connection:
            if corruption == "poll":
                connection.execute("UPDATE world_poll_state SET checks=500")
            elif corruption == "seen":
                connection.execute("UPDATE world_feed_seen SET fingerprint='damaged'")
    tables = ("world_poll_state", "world_feed_seen", "observations", "events")
    with old.connection() as connection:
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
        assert Database.schema_ddl_fingerprint(connection) == _SCHEMA_DDL_FINGERPRINTS[108]
        assert (
            Database.schema_structure_fingerprint(connection) == _SCHEMA_STRUCTURE_FINGERPRINTS[108]
        )
    upgraded = Database(old.path, strict_schema_contract=True)
    upgraded.assert_schema_contract()
    if corruption is None:
        assert WorldIntegrity(upgraded).verify(SUBJECT)["world_feed_seen"] == 1
    else:
        with pytest.raises(IntegrityError):
            WorldIntegrity(upgraded).verify(SUBJECT)
    with upgraded.connection() as connection:
        after = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(CURRENT_SCHEMA_VERSION)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='prevent_world_feed_seen_update'"
            ).fetchone()
            is not None
        )
    assert after == before
    with (
        patch("noyra.core.database.CURRENT_SCHEMA_VERSION", 108),
        pytest.raises(RuntimeError, match="newer than runtime"),
    ):
        Database(old.path)


def test_missing_seen_guard_is_not_silently_repaired(
    context: tuple[Database, SourceRecord],
) -> None:
    database, _ = context
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_world_feed_seen_update")
    with pytest.raises(SchemaContractError):
        Database(database.path, strict_schema_contract=True)
