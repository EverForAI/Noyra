from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from pydantic import SecretStr

from noyra.core.at_rest import _windows_permission_audit, validate_private_root
from noyra.core.database import CURRENT_SCHEMA_VERSION, Database
from noyra.core.errors import IntegrityError, InvalidTransitionError, NotFoundError
from noyra.core.identity import IdentityStore
from noyra.core.integrity import IntegrityRegistry
from noyra.core.secret_cleanup import SecretCleanupQueue
from noyra.core.types import content_hash
from noyra.model import CognitiveResourceGroupInput, CognitiveResourceStore
from noyra.research import SearchProviderInput, SearchProviderStore


def _database(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-gate1-secret-intents"
    IdentityStore(database).ensure(subject_id, content_hash({"seed": "gate1-secrets"}))
    return database, subject_id


def _proposal(label: str) -> SearchProviderInput:
    return SearchProviderInput(
        provider_type="brave",
        label=label,
        api_key=f"secret-for-{label}",
    )


def _cognitive_group(subject_id: str) -> CognitiveResourceGroupInput:
    return CognitiveResourceGroupInput(
        pool="economy",
        label=f"gate1-{subject_id}",
        base_url="https://models.example/v1",
        model="gate1-model",
        api_keys=(SecretStr("gate1-cognitive-key"),),
    )


def test_create_intent_without_database_row_removes_crash_orphan(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    secret_dir = validate_private_root(tmp_path / "search", create=True)
    queue = SecretCleanupQueue(database)
    intent_id = queue.prepare_create(subject_id, "search", "resource-1", "resource-1.key")
    secret_path = secret_dir / "resource-1.key"
    secret_path.write_text("orphan-secret", encoding="utf-8")
    queue.mark_file_ready(intent_id)

    result = queue.reconcile(subject_id, "search", secret_dir)

    assert result["removed"] == 1
    assert not secret_path.exists()
    with database.connection() as connection:
        state = connection.execute(
            "SELECT state FROM secret_file_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()[0]
    assert state == "removed"


def test_database_commit_before_intent_finalize_recovers_to_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, subject_id = _database(tmp_path)
    secret_dir = tmp_path / "search"
    store = SearchProviderStore(database, secret_dir, repair_on_init=False)

    def interrupt_finalize(_: str) -> None:
        raise RuntimeError("simulated crash after SQLite commit")

    monkeypatch.setattr(store.secret_cleanup, "mark_committed", interrupt_finalize)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.configure(subject_id, _proposal("commit-window"), actor="operator")
    monkeypatch.undo()

    restarted = SearchProviderStore(database, secret_dir)
    record = restarted.list(subject_id)[0]
    assert restarted.api_key(record.config_id, subject_id=subject_id) == "secret-for-commit-window"
    with database.connection() as connection:
        state = connection.execute(
            "SELECT state FROM secret_file_intents WHERE resource_id = ? AND operation = 'create'",
            (record.config_id,),
        ).fetchone()[0]
    assert state == "committed"


def test_cognitive_add_keys_uses_durable_secret_intent(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    store = CognitiveResourceStore(database, tmp_path / "models", repair_on_init=False)
    group = store.configure(subject_id, _cognitive_group(subject_id), actor="operator")

    added = store.add_keys(
        group.group_id,
        (SecretStr("gate1-added-key"),),
        actor="operator",
        subject_id=subject_id,
    )
    added_fingerprint = content_hash({"api_key": "gate1-added-key"})
    added_key = next(
        key
        for key in store.keys(group.group_id, subject_id=subject_id)
        if key.key_fingerprint == added_fingerprint
    )
    assert added.group_id == group.group_id
    with database.connection() as connection:
        row = connection.execute(
            "SELECT state, operation FROM secret_file_intents WHERE resource_id = ?",
            (added_key.key_id,),
        ).fetchone()
    assert row["state"] == "committed"
    assert row["operation"] == "create"
    assert (store.secret_dir / f"{added_key.key_id}.key").is_file()


def test_cognitive_add_keys_failure_removes_file_and_closes_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, subject_id = _database(tmp_path)
    store = CognitiveResourceStore(database, tmp_path / "models", repair_on_init=False)
    group = store.configure(subject_id, _cognitive_group(subject_id), actor="operator")
    existing_reference = f"{store.keys(group.group_id, subject_id=subject_id)[0].key_id}.key"

    def fail_insert(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected add-key transaction failure")

    monkeypatch.setattr(store, "_insert_key_event", fail_insert)
    with pytest.raises(RuntimeError, match="injected add-key"):
        store.add_keys(
            group.group_id,
            (SecretStr("gate1-failing-key"),),
            actor="operator",
            subject_id=subject_id,
        )
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT state FROM secret_file_intents WHERE fingerprint = ?",
            (content_hash({"api_key": "gate1-failing-key"}),),
        ).fetchall()
    assert rows and all(row["state"] == "removed" for row in rows)
    assert not any(
        path.name.endswith(".key") and path.name != existing_reference
        for path in store.secret_dir.iterdir()
    )


def test_cognitive_add_keys_rejects_revoked_group_without_orphan_secret(
    tmp_path: Path,
) -> None:
    database, subject_id = _database(tmp_path)
    store = CognitiveResourceStore(database, tmp_path / "models", repair_on_init=False)
    group = store.configure(subject_id, _cognitive_group(subject_id), actor="operator")
    store.revoke(
        group.group_id,
        reason="retired route",
        actor="operator",
        subject_id=subject_id,
    )
    before = {path.name for path in store.secret_dir.iterdir()}

    with pytest.raises(ValueError, match="revoked cognitive resources"):
        store.add_keys(
            group.group_id,
            (SecretStr("must-not-be-written"),),
            actor="operator",
            subject_id=subject_id,
        )

    assert {path.name for path in store.secret_dir.iterdir()} == before
    assert all(key.status == "revoked" for key in store.keys(group.group_id, subject_id=subject_id))


def test_reconciliation_is_resource_scoped_and_cannot_consume_other_intents(
    tmp_path: Path,
) -> None:
    database, subject_id = _database(tmp_path)
    search_dir = validate_private_root(tmp_path / "search", create=True)
    embedding_dir = validate_private_root(tmp_path / "embedding", create=True)
    queue = SecretCleanupQueue(database)
    intent_id = queue.prepare_create(subject_id, "search", "search-1", "shared.key")
    search_path = search_dir / "shared.key"
    search_path.write_text("search-secret", encoding="utf-8")
    queue.mark_file_ready(intent_id)

    queue.reconcile(subject_id, "embedding", embedding_dir)

    assert search_path.exists()
    with database.connection() as connection:
        state = connection.execute(
            "SELECT state FROM secret_file_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()[0]
    assert state == "file_ready"
    queue.reconcile(subject_id, "search", search_dir)
    assert not search_path.exists()


def test_bounded_reconciliation_never_deletes_unscanned_live_files(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    store = SearchProviderStore(database, tmp_path / "search", repair_on_init=False)
    records = [
        store.configure(subject_id, _proposal(f"provider-{index}"), actor="operator")
        for index in range(2)
    ]
    paths = [store.secret_dir / f"{record.config_id}.key" for record in records]
    orphan = store.secret_dir / "zz-bounded-orphan.key"
    orphan.write_text("orphan", encoding="utf-8")
    # Force the intent prefix and table row prefix to refer to different
    # resources, reproducing the independent bounded-ordering hazard.
    with database.transaction() as connection:
        connection.execute(
            "UPDATE secret_file_intents SET updated_at = '2000-01-01T00:00:00.000+00:00' "
            "WHERE resource_id = ?",
            (records[1].config_id,),
        )

    result = store.secret_cleanup.reconcile(subject_id, "search", store.secret_dir, limit=1)

    assert all(path.exists() for path in paths)
    assert result["orphans"] == 1
    assert not orphan.exists()
    assert store.secret_cleanup.health(subject_id, "search")["status"] == "ok"


def test_delete_intent_conflicting_with_active_row_fails_without_deleting_secret(
    tmp_path: Path,
) -> None:
    database, subject_id = _database(tmp_path)
    store = SearchProviderStore(database, tmp_path / "search", repair_on_init=False)
    record = store.configure(subject_id, _proposal("active"), actor="operator")
    reference = f"{record.config_id}.key"
    path = store.secret_dir / reference
    store.secret_cleanup.prepare_delete(subject_id, "search", record.config_id, reference)

    result = store.secret_cleanup.reconcile(subject_id, "search", store.secret_dir)

    assert result["failed"] == 1
    assert path.exists()
    assert store.api_key(record.config_id, subject_id=subject_id) == "secret-for-active"
    health = store.secret_cleanup.health(subject_id, "search")
    assert health["pending"] == 1
    assert health["failed"] == 1


def test_legacy_cleanup_queue_cannot_delete_active_secret_alias(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    store = SearchProviderStore(database, tmp_path / "search", repair_on_init=False)
    record = store.configure(subject_id, _proposal("queue-alias-owner"), actor="operator")
    reference = f"{record.config_id}.key"
    path = store.secret_dir / reference
    store.secret_cleanup.enqueue(
        subject_id,
        "search",
        "bogus-queue-resource",
        reference,
        RuntimeError("tampered cleanup queue"),
    )

    repaired = store.secret_cleanup.repair(None, "search", store.secret_dir)

    assert repaired == 0
    assert path.exists()
    with database.connection() as connection:
        status = connection.execute(
            "SELECT status FROM secret_cleanup_queue WHERE resource_id = ?",
            ("bogus-queue-resource",),
        ).fetchone()[0]
    assert status == "failed"


def test_reference_alias_intent_cannot_delete_another_live_secret(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    store = SearchProviderStore(database, tmp_path / "search", repair_on_init=False)
    record = store.configure(subject_id, _proposal("alias-owner"), actor="operator")
    reference = f"{record.config_id}.key"
    path = store.secret_dir / reference
    queue = store.secret_cleanup
    # Simulate a legacy/tampered database whose binding trigger was absent;
    # reconciliation must still fail closed before unlinking the live file.
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER validate_secret_file_intent_reference_binding")
        connection.execute("DROP TRIGGER validate_secret_file_intent_intent_binding")
    intent_id = queue.prepare_create(subject_id, "search", "nonexistent-resource", reference)
    queue.mark_file_ready(intent_id)

    result = queue.reconcile(subject_id, "search", store.secret_dir)

    assert result["removed"] == 0
    assert result["failed"] == 1
    assert path.exists()
    report = IntegrityRegistry().run(
        database,
        subject_id,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=15,
        check_ids=("interaction.transport",),
    )
    assert report.checks[0].status == "corrupt"


def test_database_trigger_rejects_reference_alias_on_new_intent(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    store = SearchProviderStore(database, tmp_path / "search", repair_on_init=False)
    record = store.configure(subject_id, _proposal("trigger-owner"), actor="operator")
    with pytest.raises(sqlite3.IntegrityError, match="already bound"):
        store.secret_cleanup.prepare_create(
            subject_id,
            "search",
            "trigger-alias",
            f"{record.config_id}.key",
        )


def test_database_trigger_rejects_reference_alias_between_orphan_intents(
    tmp_path: Path,
) -> None:
    database, subject_id = _database(tmp_path)
    queue = SecretCleanupQueue(database)
    queue.prepare_create(subject_id, "search", "orphan-owner-a", "orphan-shared.key")
    with pytest.raises(sqlite3.IntegrityError, match="another intent"):
        queue.prepare_create(subject_id, "search", "orphan-owner-b", "orphan-shared.key")


def test_successful_revoke_completes_delete_intent_and_health(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    store = SearchProviderStore(database, tmp_path / "search", repair_on_init=False)
    record = store.configure(subject_id, _proposal("retired"), actor="operator")
    path = store.secret_dir / f"{record.config_id}.key"

    store.revoke(
        record.config_id,
        reason="retired",
        actor="operator",
        subject_id=subject_id,
    )

    assert not path.exists()
    assert store.secret_cleanup.health(subject_id, "search") == {
        "status": "ok",
        "pending": 0,
        "failed": 0,
        "max_attempts": 0,
        "oldest_pending_at": None,
    }


def test_created_secret_root_is_private(tmp_path: Path) -> None:
    root = validate_private_root(tmp_path / "private-secrets", create=True)
    if os.name == "nt":
        assert _windows_permission_audit(root)[0]
    else:
        assert root.stat().st_mode & 0o777 == 0o700


def test_secret_file_walk_does_not_descend_reparse_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = validate_private_root(tmp_path / "private-secrets", create=True)
    nested = root / "nested"
    nested.mkdir()
    (nested / "outside.key").write_text("must not be visited", encoding="utf-8")
    monkeypatch.setattr(
        "noyra.core.secret_cleanup._is_reparse_entry",
        lambda path: Path(path) == nested,
    )

    assert list(SecretCleanupQueue._files(root)) == []


def test_intent_identity_cannot_be_reassigned_across_subjects(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    other_subject = "Noyra-gate1-secret-intents-other"
    IdentityStore(database).ensure(other_subject, content_hash({"seed": other_subject}))
    queue = SecretCleanupQueue(database)
    queue.prepare_create(subject_id, "search", "shared-id", "shared.key")

    with pytest.raises(IntegrityError, match="subject boundary"):
        queue.prepare_create(other_subject, "search", "shared-id", "shared.key")
    with database.connection() as connection:
        owner = connection.execute(
            "SELECT subject_id FROM secret_file_intents WHERE resource_id = 'shared-id'"
        ).fetchone()[0]
    assert owner == subject_id


def test_secret_intent_state_machine_rejects_terminal_resurrection(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    queue = SecretCleanupQueue(database)
    intent_id = queue.prepare_create(subject_id, "search", "stateful", "stateful.key")
    queue.mark_file_ready(intent_id)
    queue.mark_committed(intent_id)
    with pytest.raises((IntegrityError, InvalidTransitionError), match="transition"):
        queue.mark(intent_id, "prepared")
    queue.mark_removed(intent_id)
    with pytest.raises((IntegrityError, InvalidTransitionError), match="transition"):
        queue.mark(intent_id, "failed")


def test_reconciliation_restores_failed_create_through_prepared_state(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    store = SearchProviderStore(database, tmp_path / "search", repair_on_init=False)
    record = store.configure(subject_id, _proposal("restored"), actor="operator")
    reference = f"{record.config_id}.key"
    path = store.secret_dir / reference
    secret = path.read_bytes()
    path.unlink()

    first = store.secret_cleanup.reconcile(subject_id, "search", store.secret_dir)
    assert first["failed"] == 1
    path.write_bytes(secret)

    restored = store.secret_cleanup.reconcile(subject_id, "search", store.secret_dir)

    assert restored["committed"] == 1
    with database.connection() as connection:
        state = connection.execute(
            "SELECT state FROM secret_file_intents WHERE resource_id = ? AND operation = 'create'",
            (record.config_id,),
        ).fetchone()[0]
    assert state == "committed"
    assert store.api_key(record.config_id, subject_id=subject_id) == "secret-for-restored"


def test_secret_intent_mark_rejects_unknown_state_and_missing_intent(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    queue = SecretCleanupQueue(database)
    intent_id = queue.prepare_create(subject_id, "search", "invalid-state", "invalid-state.key")

    with pytest.raises(ValueError, match="state is invalid"):
        queue.mark(intent_id, "not-a-secret-state")
    with pytest.raises(NotFoundError, match="secret intent not found"):
        queue.mark("missing-secret-intent", "prepared")

    with database.connection() as connection:
        state = connection.execute(
            "SELECT state FROM secret_file_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()[0]
    assert state == "prepared"


def test_secret_intent_sqlite_trigger_blocks_removed_rollback(tmp_path: Path) -> None:
    database, subject_id = _database(tmp_path)
    queue = SecretCleanupQueue(database)
    intent_id = queue.prepare_create(subject_id, "search", "sqlite-terminal", "sqlite-terminal.key")
    queue.mark_removed(intent_id)

    with (
        pytest.raises(sqlite3.IntegrityError, match="transition"),
        database.transaction() as connection,
    ):
        connection.execute(
            "UPDATE secret_file_intents SET state = 'prepared' WHERE intent_id = ?",
            (intent_id,),
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("operation", "delete"),
        ("resource_type", "transport"),
        ("resource_id", "reassigned-resource"),
        ("secret_reference", "reassigned.key"),
        ("created_at", "2000-01-01T00:00:00.000+00:00"),
    ],
)
def test_secret_intent_sqlite_trigger_blocks_identity_reassignment(
    tmp_path: Path, column: str, value: str
) -> None:
    database, subject_id = _database(tmp_path)
    intent_id = SecretCleanupQueue(database).prepare_create(
        subject_id, "search", "immutable-resource", "immutable.key"
    )

    with (
        pytest.raises(sqlite3.IntegrityError, match="identity is immutable"),
        database.transaction() as connection,
    ):
        connection.execute(
            f"UPDATE secret_file_intents SET {column} = ? WHERE intent_id = ?",
            (value, intent_id),
        )


def test_secret_intent_rows_are_append_only_even_for_replace_conflicts(
    tmp_path: Path,
) -> None:
    database, subject_id = _database(tmp_path)
    intent_id = SecretCleanupQueue(database).prepare_create(
        subject_id, "search", "append-only-resource", "append-only.key"
    )

    with (
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
        database.transaction() as connection,
    ):
        connection.execute("DELETE FROM secret_file_intents WHERE intent_id = ?", (intent_id,))

    with (
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
        database.transaction() as connection,
    ):
        connection.execute(
            """INSERT OR REPLACE INTO secret_file_intents(
                intent_id, subject_id, resource_type, resource_id, secret_reference,
                operation, state, created_at, updated_at
            ) VALUES (?, ?, 'search', 'append-only-resource', 'append-only.key',
                      'create', 'removed', 'tampered', 'tampered')""",
            ("tampered-intent", subject_id),
        )

    with database.connection() as connection:
        row = connection.execute(
            "SELECT intent_id, state FROM secret_file_intents WHERE resource_id = ?",
            ("append-only-resource",),
        ).fetchone()
    assert row is not None
    assert row["intent_id"] == intent_id
    assert row["state"] == "prepared"


def test_schema42_optional_pass_repairs_missing_secret_intent_trigger(tmp_path: Path) -> None:
    path = tmp_path / "trigger-repair.sqlite3"
    database = Database(path)
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER validate_secret_file_intent_transition")
        connection.execute("DROP TRIGGER validate_secret_file_intent_operation_state")

    repaired = Database(path)
    with repaired.connection() as connection:
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND (name LIKE 'validate_secret_file_intent_%' "
                "OR name = 'prevent_secret_file_intent_delete')"
            ).fetchall()
        }
    assert {
        "validate_secret_file_intent_transition",
        "validate_secret_file_intent_operation_state",
        "validate_secret_file_intent_identity_immutable",
        "prevent_secret_file_intent_delete",
        "validate_secret_file_intent_intent_binding",
    }.issubset(names)


def test_secret_publication_failure_removes_file_and_marks_intent_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, subject_id = _database(tmp_path)
    store = SearchProviderStore(database, tmp_path / "search", repair_on_init=False)

    def fail_mark(_: str) -> None:
        raise OSError("injected mark failure")

    monkeypatch.setattr(store.secret_cleanup, "mark_file_ready", fail_mark)
    with pytest.raises(OSError, match="injected"):
        store.configure(subject_id, _proposal("publication-failure"), actor="operator")
    with database.connection() as connection:
        row = connection.execute(
            "SELECT state FROM secret_file_intents WHERE resource_type = 'search'"
        ).fetchone()
    assert row["state"] == "removed"
    assert not list(store.secret_dir.iterdir())


def test_schema_42_migrates_secret_journal_and_archive_generation_trigger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "migration.sqlite3"
    database = Database(path)
    with database.transaction() as connection:
        connection.execute("DROP TABLE secret_file_intents")
        connection.execute("DROP TRIGGER validate_archive_keyring_generation_insert")
        connection.execute("DROP TRIGGER validate_public_post_moderation_transition")
        connection.execute("DROP TRIGGER validate_public_post_identity_insert")
        connection.execute("DROP TRIGGER prevent_public_post_identity_update")
        connection.execute("DROP INDEX uq_public_post_moderation_revision")
        connection.execute("DROP INDEX uq_public_post_moderation_idempotency")
        connection.execute("DROP INDEX idx_public_post_rate_events_subject_time")
        connection.execute("DROP INDEX idx_public_post_captcha_issue_subject_time")
        connection.execute("ALTER TABLE public_post_moderation_events DROP COLUMN idempotency_key")
        connection.execute(
            "ALTER TABLE public_post_moderation_events DROP COLUMN previous_event_id"
        )
        connection.execute("ALTER TABLE public_post_moderation_events DROP COLUMN revision")
        connection.execute("ALTER TABLE public_posts DROP COLUMN author_provenance")
        connection.execute("ALTER TABLE public_posts DROP COLUMN identity_hash")
        connection.execute(
            "ALTER TABLE public_post_controls DROP COLUMN captcha_global_rate_per_minute"
        )
        connection.execute(
            "ALTER TABLE public_post_controls DROP COLUMN captcha_issue_limit_per_hour"
        )
        connection.execute("ALTER TABLE public_post_controls DROP COLUMN storage_cap_bytes")
        connection.execute("ALTER TABLE interaction_transports DROP COLUMN endpoint_digest")
        connection.execute("ALTER TABLE interaction_transports DROP COLUMN endpoint_contract")
        connection.execute("UPDATE schema_meta SET value = '41' WHERE key = 'schema_version'")

    migrated = Database(path)
    with migrated.connection() as connection:
        version = int(
            connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        )
        table = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'secret_file_intents'"
        ).fetchone()
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'validate_archive_keyring_generation_insert'"
        ).fetchone()
    assert version == CURRENT_SCHEMA_VERSION
    assert table is not None and "operation IN ('create', 'delete')" in str(table[0])
    assert trigger is not None and "MAX(generation) + 1" in str(trigger[0])
