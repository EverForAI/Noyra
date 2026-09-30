from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.actions import ActionLedger
from noyra.core.provider_health import ProviderHealthStore
from noyra.core.retention import (
    RETENTION_REGISTRY,
    RetentionManager,
    RetentionSettings,
    retention_registry_diagnostics,
)
from noyra.core.types import content_hash
from noyra.research.provider import SearchProviderStore
from noyra.research.types import SearchProviderInput


def test_settings_defaults_and_bounds():
    settings = RetentionSettings.from_env({})
    assert settings.health_days == 30
    assert settings.batch_size == 500
    assert settings.run_history == 100
    assert settings.search_use_hours == 2
    assert settings.runtime_days == 90
    with pytest.raises(ValueError):
        RetentionSettings.from_env({"NOYRA_RETENTION_BATCH_SIZE": "501"})
    with pytest.raises(ValueError):
        RetentionSettings.from_env({"NOYRA_RETENTION_SEARCH_USE_HOURS": "1"})


def test_retention_run_history_is_bounded(tmp_path):
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention-history"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    manager = RetentionManager(db, RetentionSettings(run_history=2))

    for _ in range(4):
        assert manager.run_batch(subject)["failed_reason"] is None

    with db.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM retention_runs WHERE subject_id=?", (subject,)
        ).fetchone()[0]
    assert count == 2


def test_run_batch_is_bounded_and_removes_old_health_rows(tmp_path):
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    health = ProviderHealthStore(db, failure_threshold=1)
    health.record_attempt(subject, "model", "provider", "attempt", True, 10, None)
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE provider_health_buckets SET bucket_start=?, state_hash=? WHERE subject_id=?",
            (old, content_hash({"legacy": True}), subject),
        )
    result = RetentionManager(db).run_batch(subject, batch_size=1)
    assert sum(result["deleted_by_table"].values()) <= 1
    assert result["failed_reason"] is None


def test_run_batch_prunes_only_expired_search_rate_limit_records(tmp_path):
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-search-retention"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    provider = SearchProviderStore(db, tmp_path / "secrets").configure(
        subject,
        SearchProviderInput(
            provider_type="brave",
            label="retention provider",
            api_key="retention-test-key",
        ),
        actor="operator",
    )
    actions = ActionLedger(db)
    now = datetime.now(UTC)
    for label, created_at in (
        ("expired", now - timedelta(hours=5)),
        ("recent", now - timedelta(hours=1)),
    ):
        query_hash = content_hash({"query": label})
        resource = {"config_id": provider.config_id, "query_hash": query_hash, "limit": 1}
        action = actions.prepare(
            subject,
            "search",
            "search_api:brave",
            "[private-search-query]",
            resource,
            expected_outcome="sources",
            resource_cost=resource,
            idempotency_key=f"retention-search:{label}",
        )
        with db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO search_provider_uses(
                    use_id, config_id, subject_id, action_id, query_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"use-{label}",
                    provider.config_id,
                    subject,
                    action.action_id,
                    query_hash,
                    created_at.isoformat(),
                ),
            )

    manager = RetentionManager(db, RetentionSettings(search_use_hours=2))
    assert manager.estimate(subject, now=now)["search_provider_uses"] == 1
    result = manager.run_batch(subject, now=now, batch_size=1)

    assert result["failed_reason"] is None
    assert result["deleted_by_table"]["search_provider_uses"] == 1
    with db.connection() as connection:
        remaining = connection.execute(
            "SELECT use_id FROM search_provider_uses WHERE subject_id=? ORDER BY use_id",
            (subject,),
        ).fetchall()
    assert [row["use_id"] for row in remaining] == ["use-recent"]


def test_retention_next_cursor_is_per_table_and_resumes_each_table(tmp_path):
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention-cursors"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    health = ProviderHealthStore(db, failure_threshold=1)
    for provider in ("a", "b"):
        health.record_attempt(subject, "model", provider, f"attempt-{provider}", True, 10, None)
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE provider_health_buckets SET bucket_start=?, state_hash=? WHERE subject_id=?",
            (old, content_hash({"legacy": True}), subject),
        )
    manager = RetentionManager(db, RetentionSettings(batch_size=1))

    first = manager.run_batch(subject, batch_size=1)
    second = manager.run_batch(subject, batch_size=1)

    assert first["failed_reason"] is None
    assert second["failed_reason"] is None
    assert set(first["next_cursor"]) >= {
        "provider_health_buckets",
        "search_provider_uses",
        "provider_health_attempts",
    }
    assert first["next_cursor"]["provider_health_buckets"]["cutoff"]
    assert second["next_cursor"]["provider_health_buckets"]["deleted"] >= 0


def test_retention_failure_is_persisted_for_diagnostics(tmp_path):
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention-failure"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    manager = RetentionManager(db)

    with patch.object(manager, "_delete_table", side_effect=RuntimeError("locked")):
        result = manager.run_batch(subject)

    assert result["failed_reason"] == "RuntimeError"
    latest = manager.latest(subject)
    assert latest is not None
    assert latest["failed_reason"] == "RuntimeError"
    assert latest["failure_stage"] == "delete"


def test_retention_resumes_from_previous_keyset_cursor(tmp_path):
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention-resume"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    health = ProviderHealthStore(db, failure_threshold=1)
    for provider in ("a", "b"):
        health.record_attempt(subject, "model", provider, f"attempt-{provider}", True, 10, None)
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE provider_health_buckets SET bucket_start=?, state_hash=? WHERE subject_id=?",
            (old, content_hash({"legacy": True}), subject),
        )
    manager = RetentionManager(db, RetentionSettings(batch_size=1))
    observed: list[object] = []
    original = manager._delete_table

    def wrapped(connection, table, subject_id, cutoff, limit, cursor):
        if table == "provider_health_buckets":
            observed.append(cursor)
        return original(connection, table, subject_id, cutoff, limit, cursor)

    with patch.object(manager, "_delete_table", side_effect=wrapped):
        assert manager.run_batch(subject, batch_size=1)["failed_reason"] is None
        assert manager.run_batch(subject, batch_size=1)["failed_reason"] is None

    assert observed[0] is None
    assert isinstance(observed[1], dict)
    assert observed[1]["bucket_start"] == old


def test_retention_never_schedules_append_only_route_history(tmp_path):
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention-route-history"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    manager = RetentionManager(db)
    calls: list[str] = []
    original = manager._delete_table

    def wrapped(connection, table, subject_id, cutoff, limit, cursor):
        calls.append(table)
        return original(connection, table, subject_id, cutoff, limit, cursor)

    with patch.object(manager, "_delete_table", side_effect=wrapped):
        result = manager.run_batch(subject)
    assert result["failed_reason"] is None
    assert "cognitive_route_attempts" not in calls
    assert "cognitive_route_outcomes" not in calls


def test_long_lived_tables_have_explicit_retention_classification():
    expected = {
        "cognitive_route_decisions",
        "cognitive_route_attempts",
        "cognitive_route_outcomes",
        "model_calls",
        "model_attempts",
        "research_search_runs",
        "action_deliberation_runs",
        "behavior_logs",
        "provider_health_buckets",
        "provider_health_attempts",
        "search_provider_uses",
        "retention_runs",
    }
    registry = {item.table: item for item in RETENTION_REGISTRY}
    assert expected <= set(registry)
    assert all(item.retention_class for item in registry.values())
    assert all(item.retention_action in {"preserve", "delete"} for item in registry.values())


def test_append_only_evidence_is_never_in_delete_registry():
    diagnostics = retention_registry_diagnostics()
    assert diagnostics["unclassified"] == ()
    assert diagnostics["delete_tables"] == (
        "provider_health_buckets",
        "provider_health_attempts",
        "search_provider_uses",
        "retention_runs",
    )
    registry = {item.table: item for item in RETENTION_REGISTRY}
    for table in (
        "cognitive_route_decisions",
        "cognitive_route_attempts",
        "cognitive_route_outcomes",
        "model_calls",
        "model_attempts",
        "research_search_runs",
        "action_deliberation_runs",
        "behavior_logs",
    ):
        assert registry[table].retention_action == "preserve"


def test_retention_projection_uses_registry_cutoff_metadata(tmp_path):
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention-registry"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    projection = RetentionManager(db).estimate(subject)
    assert set(projection) >= {
        "provider_health_buckets",
        "search_provider_uses",
        "provider_health_attempts",
        "retention_runs",
    }
