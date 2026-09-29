from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.provider_health import ProviderHealthStore
from noyra.core.types import content_hash


def fixture(tmp_path: Path):
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-provider-health"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    return db, subject


def test_health_persists_only_hourly_aggregates_and_latency(tmp_path: Path):
    db, subject = fixture(tmp_path)
    store = ProviderHealthStore(db)
    assert store.record_attempt(subject, "model", "provider-a", "attempt-1", True, 100, None)
    assert store.record_attempt(subject, "model", "provider-a", "attempt-2", False, 900, "timeout")
    store.record_attempt(subject, "model", "provider-a", "attempt-2", False, 300, "timeout")
    row = store.list_projection(subject, "model")[0]
    assert row["attempt_count"] == 3
    assert row["success_count"] == 1
    assert row["failure_rate"] == 2 / 3
    assert row["average_latency_ms"] == 1300 / 3
    assert row["p50_latency_ms"] == 300
    assert row["p95_latency_ms"] == 900
    assert row["error_counts"] == {"timeout": 2}
    assert row["last_success_at"]
    with db.connection() as connection:
        tables = {
            item[0]
            for item in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "provider_health_attempts" not in tables
        assert connection.execute("SELECT COUNT(*) FROM provider_health_buckets").fetchone()[0] == 1


def test_projection_rejects_tampered_health_aggregate(tmp_path: Path):
    db, subject = fixture(tmp_path)
    store = ProviderHealthStore(db)
    store.record_attempt(subject, "model", "provider-a", "attempt-1", True, 100, None)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE provider_health_buckets SET attempt_count=attempt_count+1 "
            "WHERE subject_id=? AND provider_id=?",
            (subject, "provider-a"),
        )

    with pytest.raises(IntegrityError):
        store.list_projection(subject, "model")


def test_projection_includes_the_actual_metric_window(tmp_path: Path):
    db, subject = fixture(tmp_path)
    store = ProviderHealthStore(db)
    store.record_attempt(subject, "model", "provider-a", "attempt-1", True, 100, None)
    row = store.list_projection(subject, "model")[0]
    assert row["bucket_count"] == 1
    assert row["window_start"]
    assert row["window_end"]
    assert row["window_start"] <= row["window_end"]


def test_missing_state_for_recorded_provider_fails_closed(tmp_path: Path):
    db, subject = fixture(tmp_path)
    store = ProviderHealthStore(db)
    store.record_attempt(subject, "model", "provider-a", "attempt-1", False, 100, "timeout")
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM provider_health_state WHERE subject_id=? AND provider_id=?",
            (subject, "provider-a"),
        )
    with pytest.raises(IntegrityError):
        store.route_available(subject, "model", "provider-a")


def test_probe_completion_requires_the_matching_probe_token(tmp_path: Path):
    db, subject = fixture(tmp_path)
    store = ProviderHealthStore(db, failure_threshold=1)
    store.record_attempt(subject, "model", "provider-a", "attempt-1", False, 100, "timeout")
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM provider_health_state WHERE subject_id=? AND provider_id=?",
            (subject, "provider-a"),
        ).fetchone()
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        state_hash = store._state_hash(
            subject,
            "model",
            "provider-a",
            row["state"],
            expired,
            row["probe_token"],
            row["probe_started_at"],
            row["consecutive_failures"],
            row["last_success_at"],
            row["last_failure_at"],
            row["updated_at"],
        )
        connection.execute(
            "UPDATE provider_health_state SET cooldown_until=?, state_hash=? "
            "WHERE subject_id=? AND provider_id=?",
            (expired, state_hash, subject, "provider-a"),
        )
    assert store.route_available(subject, "model", "provider-a") is True
    with db.connection() as connection:
        token = connection.execute(
            "SELECT probe_token FROM provider_health_state WHERE subject_id=? AND provider_id=?",
            (subject, "provider-a"),
        ).fetchone()[0]
    store.record_attempt(
        subject,
        "model",
        "provider-a",
        "attempt-stale",
        True,
        10,
        None,
        probe_token="wrong-token",
    )
    with db.connection() as connection:
        assert (
            connection.execute(
                "SELECT probe_token FROM provider_health_state WHERE subject_id=? AND provider_id=?",
                (subject, "provider-a"),
            ).fetchone()[0]
            == token
        )


def test_health_cooldown_recovers_with_one_automatic_probe(tmp_path: Path):
    db, subject = fixture(tmp_path)
    store = ProviderHealthStore(db, failure_threshold=1)
    store.record_attempt(
        subject, "search", "provider-b", "attempt-1", False, 50, "timeout", cooldown_seconds=120
    )
    projection = store.list_projection(subject, "search")[0]
    assert projection["state"] == "cooldown"
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM provider_health_state WHERE subject_id=? AND provider_id=?",
            (subject, "provider-b"),
        ).fetchone()
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        state_hash = store._state_hash(
            subject,
            "search",
            "provider-b",
            row["state"],
            expired,
            row["probe_token"],
            row["probe_started_at"],
            row["consecutive_failures"],
            row["last_success_at"],
            row["last_failure_at"],
            row["updated_at"],
        )
        connection.execute(
            """
            UPDATE provider_health_state SET cooldown_until=?,state_hash=?
            WHERE subject_id=? AND provider_id=?
            """,
            (expired, state_hash, subject, "provider-b"),
        )
    assert store.route_available(subject, "search", "provider-b") is True
    assert store.route_available(subject, "search", "provider-b") is False
