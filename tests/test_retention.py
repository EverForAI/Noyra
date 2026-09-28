from datetime import UTC, datetime, timedelta

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.actions import ActionLedger
from noyra.core.provider_health import ProviderHealthStore
from noyra.core.retention import RetentionManager, RetentionSettings
from noyra.core.types import content_hash
from noyra.research.provider import SearchProviderStore
from noyra.research.types import SearchProviderInput


def test_settings_defaults_and_bounds():
    settings = RetentionSettings.from_env({})
    assert settings.health_days == 30
    assert settings.batch_size == 500
    assert settings.run_history == 100
    assert settings.search_use_hours == 2
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
