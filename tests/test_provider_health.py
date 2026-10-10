import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.integrity import IntegrityRegistry
from noyra.core.provider_health import ProviderHealthStore, RoutePermit
from noyra.core.types import content_hash


def legacy_bucket() -> dict[str, Any]:
    row = {
        "subject_id": "test",
        "provider_kind": "model",
        "provider_id": "test",
        "bucket_start": "2026-10-10T00:00:00+00:00",
        "attempt_count": 2,
        "success_count": 1,
        "failure_count": 1,
        "unknown_count": 0,
        "latency_total_ms": 100,
        "last_success_at": None,
        "last_failure_at": None,
        "error_counts_json": "{}",
        "latency_samples_json": "[]",
    }
    row["state_hash"] = ProviderHealthStore._legacy_bucket_hash(row)
    return row


@pytest.mark.parametrize(
    "errors,samples",
    [
        ({"auth": -999}, [-800]),
        ({"auth": True}, []),
        ([], []),
        ({}, {}),
        ({"other": 1}, []),
        ({"auth": 3}, []),
        ({}, [101]),
        ({}, [0, 0, 0]),
        ({}, [True]),
        ({}, [0.5]),
        ({}, [0] * 129),
        (None, []),
    ],
)
@pytest.mark.parametrize("modern", [False, True])
def test_health_detail_shape_counts_and_bounds_are_authenticated(
    errors: Any,
    samples: Any,
    modern: bool,
) -> None:
    row = legacy_bucket()
    row["error_counts_json"] = json.dumps(errors)
    row["latency_samples_json"] = json.dumps(samples)
    if modern:
        row["state_hash"] = ProviderHealthStore._bucket_hash(
            "test",
            "model",
            "test",
            row["bucket_start"],
            2,
            1,
            1,
            100,
            0,
            None,
            None,
            errors,
            samples,
        )
    with pytest.raises(IntegrityError):
        ProviderHealthStore._verify_bucket(row)


def test_canonical_legacy_bucket_accepts_only_empty_added_fields() -> None:
    row = legacy_bucket()
    ProviderHealthStore._verify_bucket(row)
    row["error_counts_json"] = '{"auth":1}'
    with pytest.raises(IntegrityError):
        ProviderHealthStore._verify_bucket(row)
    row["error_counts_json"] = "{}"
    row["last_failure_at"] = "2026-10-10T00:00:00+00:00"
    with pytest.raises(IntegrityError):
        ProviderHealthStore._verify_bucket(row)


def test_legacy_state_cannot_authenticate_a_new_probe() -> None:
    row = {
        "subject_id": "test",
        "provider_kind": "model",
        "provider_id": "test",
        "state": "healthy",
        "consecutive_failures": 0,
        "updated_at": "2026-10-10T00:00:00+00:00",
        "cooldown_until": None,
        "probe_token": None,
        "probe_started_at": None,
        "last_success_at": None,
        "last_failure_at": None,
    }
    row["state_hash"] = ProviderHealthStore._legacy_state_hash(row)
    ProviderHealthStore._verify_state(row)
    row["probe_token"] = "injected"
    row["probe_started_at"] = row["updated_at"]
    with pytest.raises(IntegrityError):
        ProviderHealthStore._verify_state(row)


def test_projection_and_watchdog_share_strict_legacy_validation(tmp_path: Path) -> None:
    db, subject = fixture(tmp_path)
    store = ProviderHealthStore(db)
    store.record_attempt(subject, "model", "test", "fixture", False, 100, "timeout")
    with db.transaction() as connection:
        row = dict(connection.execute("SELECT * FROM provider_health_buckets").fetchone())
        row["state_hash"] = store._legacy_bucket_hash(row)
        connection.execute(
            "UPDATE provider_health_buckets SET state_hash=?,error_counts_json=?,"
            "latency_samples_json='[]',last_success_at=NULL,last_failure_at=NULL",
            (row["state_hash"], '{"auth":-999}'),
        )
    with pytest.raises(IntegrityError):
        store.list_projection(subject, "model")
    report = IntegrityRegistry().run(
        db,
        subject,
        tmp_path,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("operations.provider_health",),
    )
    assert report.status != "ok"
    assert any(finding.check_id == "operations.provider_health" for finding in report.findings)


def fixture(tmp_path: Path) -> Any:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-provider-health"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    return db, subject


def test_health_persists_only_hourly_aggregates_and_latency(tmp_path: Path) -> None:
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


def test_projection_rejects_tampered_health_aggregate(tmp_path: Path) -> None:
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


def test_projection_includes_the_actual_metric_window(tmp_path: Path) -> None:
    db, subject = fixture(tmp_path)
    store = ProviderHealthStore(db)
    store.record_attempt(subject, "model", "provider-a", "attempt-1", True, 100, None)
    row = store.list_projection(subject, "model")[0]
    assert row["bucket_count"] == 1
    assert row["window_start"]
    assert row["window_end"]
    assert row["window_start"] <= row["window_end"]


def test_missing_state_for_recorded_provider_fails_closed(tmp_path: Path) -> None:
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


def test_probe_completion_requires_the_matching_probe_token(tmp_path: Path) -> None:
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
                (
                    "SELECT probe_token FROM provider_health_state "
                    "WHERE subject_id=? AND provider_id=?"
                ),
                (subject, "provider-a"),
            ).fetchone()[0]
            == token
        )


def test_health_cooldown_recovers_with_one_automatic_probe(tmp_path: Path) -> None:
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


def test_unknown_outcome_is_not_counted_as_known_failure(tmp_path: Path) -> None:
    db, subject = fixture(tmp_path)
    store = ProviderHealthStore(db, failure_threshold=1)
    store.record_attempt(
        subject,
        "model",
        "provider-a",
        "attempt-unknown",
        False,
        100,
        "provider_outcome_unknown",
        outcome_unknown=True,
    )
    projection = store.list_projection(subject, "model")[0]
    assert projection["attempt_count"] == 1
    assert projection["failure_count"] == 0
    assert projection["unknown_count"] == 1
    assert projection["failure_rate"] == 0.0
    assert projection["unknown_rate"] == 1.0
    assert projection["state"] == "healthy"


def test_route_permit_is_required_to_complete_half_open_probe(tmp_path: Path) -> None:
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
    permit = store.claim_route(subject, "model", "provider-a")
    assert isinstance(permit, RoutePermit)
    assert permit.probe_token
    assert store.claim_route(subject, "model", "provider-a") is None
    store.record_attempt(
        subject,
        "model",
        "provider-a",
        "attempt-probe",
        True,
        10,
        None,
        probe_token=permit.probe_token,
    )
    assert store.route_available(subject, "model", "provider-a") is True


def test_route_permit_identity_is_verified(tmp_path: Path) -> None:
    db, subject = fixture(tmp_path)
    store = ProviderHealthStore(db)
    permit = store.claim_route(subject, "model", "provider-a")
    assert isinstance(permit, RoutePermit)
    with pytest.raises(ValueError, match="identity"):
        store.record_attempt(
            subject,
            "model",
            "provider-b",
            "attempt-mismatch",
            True,
            10,
            None,
            permit=permit,
        )
