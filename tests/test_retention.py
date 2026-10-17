import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.actions import ActionLedger
from noyra.core.backup_retention import BackupRetentionPolicy
from noyra.core.provider_health import ProviderHealthStore
from noyra.core.retention import (
    RETENTION_REGISTRY,
    RetentionManager,
    RetentionPolicyConflict,
    RetentionSettings,
    retention_registry_diagnostics,
    validate_retention_run_row,
)
from noyra.core.types import content_hash
from noyra.model.ledger import ModelLedger
from noyra.research.provider import SearchProviderStore
from noyra.research.types import SearchProviderInput


def test_settings_defaults_and_bounds() -> None:
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


def test_saved_policy_survives_restart_and_rejects_conflicts_and_corruption(tmp_path: Any) -> None:
    from noyra.core.errors import IntegrityError

    db = Database(tmp_path / "policy.sqlite3")
    subject = "Noyra-retention-policy"
    IdentityStore(db).ensure(subject, content_hash(subject))
    manager = RetentionManager(db)
    settings = asdict(RetentionSettings(health_days=7, batch_size=3))
    manager.configure(subject, settings, expected_revision=0, actor="operator")
    with pytest.raises(RetentionPolicyConflict):
        manager.configure(subject, settings, expected_revision=0, actor="operator")
    reopened = RetentionManager(Database(db.path), RetentionSettings(health_days=100))
    assert reopened.effective_settings(subject).health_days == 7
    assert reopened.run_batch(subject)["policy_revision"] == 1
    for name, value in (
        ("batch_size", 501),
        ("search_use_hours", 1),
        ("health_days", True),
        ("interval_seconds", 0),
        ("runtime_days", 1.5),
    ):
        with pytest.raises(ValueError):
            reopened.configure(
                subject, {**settings, name: value}, expected_revision=1, actor="operator"
            )
    with db.transaction() as connection:
        connection.execute("UPDATE retention_policies SET settings_json='{}'")
    with pytest.raises(IntegrityError):
        reopened.run_batch(subject)


def test_backup_policy_and_gc_run_are_durable(tmp_path: Any) -> None:
    db = Database(tmp_path / "backup-policy.sqlite3")
    subject = "Noyra-backup-policy"
    IdentityStore(db).ensure(subject, content_hash(subject))
    manager = RetentionManager(db)
    backup = BackupRetentionPolicy(keep_verified_count=3, max_age_days=14).public()
    result = manager.configure(
        subject,
        asdict(RetentionSettings()),
        expected_revision=0,
        actor="operator",
        backup_settings=backup,
        backup_expected_revision=0,
    )
    assert result["backup"]["revision"] == 1
    assert result["backup"]["settings"] == backup
    plan = manager.retention_plan(subject)
    assert plan["backup"]["policy"] == backup
    run_id = manager.record_backup_gc_run(
        subject,
        plan_id=plan["backup"].get("plan_id", "0" * 64),
        status="applied",
        cursor=plan["backup"].get("cursor", {}),
        deleted_count=0,
    )
    with db.connection() as connection:
        row = connection.execute(
            "SELECT status,plan_id FROM backup_retention_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    assert row["status"] == "applied"
    assert len(row["plan_id"]) == 64


def test_retention_settings_http_auth_revision_and_validation(tmp_path: Any) -> None:
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from pydantic import SecretStr

    from noyra.core import SubjectKernel
    from noyra.service import NoyraHTTPServer, ServiceSettings

    token = "test-token-sufficient-entropy-for-retention"
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    settings = ServiceSettings(
        data_dir=tmp_path,
        backup_root=backup_root,
        subject_id="retention-http",
        genesis_hash=content_hash("http"),
        port=0,
        admin_token=SecretStr(token),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    http = NoyraHTTPServer(kernel, settings)
    http.start()
    try:
        address = f"http://127.0.0.1:{http.address[1]}/api/admin/retention/settings"
        payload: dict[str, Any] = {
            "settings": asdict(RetentionSettings(health_days=14)),
            "expected_revision": 0,
        }
        headers = {"Content-Type": "application/json"}

        def submit() -> Any:
            return urlopen(
                Request(address, data=json.dumps(payload).encode(), headers=headers), timeout=10
            )

        with pytest.raises(HTTPError) as unauthorized:
            submit()
        assert unauthorized.value.code == 401
        headers["Authorization"] = "Bearer " + token
        with submit() as response:
            result = json.loads(response.read())
        assert result["revision"] == 1
        with urlopen(
            Request(
                f"http://127.0.0.1:{http.address[1]}/api/admin/retention",
                headers=headers,
            ),
            timeout=10,
        ) as response:
            status = json.loads(response.read())
        assert status["sqlite_space"]["page_size"] > 0
        assert status["plan"]["backup"]["status"] == "planned"
        assert status["plan"]["apply_allowed"] is False
        with pytest.raises(HTTPError) as conflict:
            submit()
        assert conflict.value.code == 409
        payload["settings"]["batch_size"] = 501
        with pytest.raises(HTTPError) as invalid:
            submit()
        assert invalid.value.code == 400
    finally:
        http.close()
        kernel.close()


def test_retention_run_history_is_bounded(tmp_path: Any) -> None:
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


def test_retention_reports_sqlite_space_without_vacuum_side_effect(tmp_path: Any) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention-space"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    with db.connection() as connection:
        connection.execute("CREATE TABLE space_probe(payload BLOB NOT NULL)")
        connection.executemany(
            "INSERT INTO space_probe(payload) VALUES (zeroblob(4096))", [() for _ in range(32)]
        )
        connection.commit()
        connection.execute("DELETE FROM space_probe")
        connection.commit()
        before = tuple(
            connection.execute(f"PRAGMA {pragma}").fetchone()[0]
            for pragma in ("page_count", "freelist_count")
        )
    manager = RetentionManager(db)
    metrics = manager.sqlite_space()
    with db.connection() as connection:
        after = tuple(
            connection.execute(f"PRAGMA {pragma}").fetchone()[0]
            for pragma in ("page_count", "freelist_count")
        )

    physical_bytes = metrics["physical_bytes"]
    wal_bytes = metrics["wal_bytes"]
    shm_bytes = metrics["shm_bytes"]
    page_size = metrics["page_size"]
    page_count = metrics["page_count"]
    freelist_pages = metrics["freelist_pages"]
    freelist_bytes = metrics["freelist_bytes"]
    assert metrics["database_bytes"] == db.path.stat().st_size
    assert physical_bytes is not None and physical_bytes >= metrics["database_bytes"]
    assert wal_bytes is not None and wal_bytes >= 0
    assert shm_bytes is not None and shm_bytes >= 0
    assert page_size is not None and page_size > 0
    assert page_count is not None and page_count > 0
    assert freelist_pages is not None and freelist_pages >= 0
    assert freelist_bytes == page_size * freelist_pages
    assert before == after
    assert freelist_pages is not None and freelist_pages > 0


def test_retention_90_day_cutoff_deletes_only_rows_strictly_older_than_cutoff(
    tmp_path: Any,
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention-cutoff-90d"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    health = ProviderHealthStore(db)
    now = datetime(2026, 10, 18, 12, tzinfo=UTC)
    cutoff = now - timedelta(days=90)
    samples = {
        "older": cutoff - timedelta(seconds=1),
        "at-cutoff": cutoff,
        "newer": cutoff + timedelta(seconds=1),
    }
    for provider in samples:
        health.record_attempt(subject, "model", provider, f"attempt-{provider}", True, 1, None)
    with db.transaction() as connection:
        for provider, timestamp in samples.items():
            connection.execute(
                "UPDATE provider_health_buckets SET bucket_start=?, state_hash=? "
                "WHERE subject_id=? AND provider_id=?",
                (timestamp.isoformat(), content_hash({"provider": provider}), subject, provider),
            )

    result = RetentionManager(db, RetentionSettings(health_days=90)).run_batch(
        subject, now=now, batch_size=50
    )

    assert result["failed_reason"] is None
    assert result["deleted_by_table"]["provider_health_buckets"] == 1
    with db.connection() as connection:
        remaining = {
            row["provider_id"]: row["bucket_start"]
            for row in connection.execute(
                "SELECT provider_id, bucket_start FROM provider_health_buckets WHERE subject_id=?",
                (subject,),
            )
        }
    assert set(remaining) == {"at-cutoff", "newer"}
    assert remaining["at-cutoff"] == cutoff.isoformat()


def test_retention_compacts_cold_model_payloads_without_deleting_ledger_rows(
    tmp_path: Any,
) -> None:
    from noyra.model.ledger import ModelLedger

    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention-model-payloads"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    ledger = ModelLedger(db)
    call, _ = ledger.prepare_call(
        subject,
        "provider",
        "model",
        "retention-test",
        content_hash({"prompt": "x"}),
        "retention-call-1",
        request={"prompt": "x"},
    )
    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    payload = '{"prompt":"' + ("x" * 5000) + '"}'
    with db.transaction() as connection:
        connection.execute(
            "UPDATE model_calls SET status='failed', request_json=?, created_at=? WHERE call_id=?",
            (payload, old, call.call_id),
        )
    result = RetentionManager(db, RetentionSettings(runtime_days=90)).run_batch(subject)
    assert result["compacted_by_table"]["model_calls"] == 1
    with db.connection() as connection:
        row = connection.execute(
            "SELECT request_json FROM model_calls WHERE call_id=?", (call.call_id,)
        ).fetchone()
    assert row is not None
    assert row["request_json"] != payload


def test_compaction_advances_past_small_rows_and_survives_restart(tmp_path: Any) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-compaction-progress"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    ledger = ModelLedger(db)
    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    calls = []
    for index, prompt in enumerate(("small", "x" * 5000, "y" * 5000)):
        call, _ = ledger.prepare_call(
            subject,
            "provider",
            "model",
            "test",
            content_hash({"prompt": prompt}),
            f"progress-{index}",
            request={"prompt": prompt},
        )
        with db.transaction() as connection:
            connection.execute(
                "UPDATE model_calls SET call_id=?,status='failed',created_at=? WHERE call_id=?",
                (f"mcall_ordered_{index}", old, call.call_id),
            )
        calls.append(f"mcall_ordered_{index}")
    assert ledger.compress_cold_payloads(subject, limit=1) == 0
    restarted = ModelLedger(Database(db.path))
    assert restarted.compress_cold_payloads(subject, limit=1) == 1
    assert restarted.compress_cold_payloads(subject, limit=1) == 1
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT request_json FROM model_calls ORDER BY call_id"
        ).fetchall()
    assert rows[1][0].startswith("noyra-zlib-b64:")
    assert rows[2][0].startswith("noyra-zlib-b64:")
    # A finite sweep restarts, including records backdated behind its old cursor.
    with db.transaction() as connection:
        connection.execute(
            "UPDATE model_calls SET request_json=? WHERE call_id=?",
            ('{"prompt":"' + "z" * 5000 + '"}', calls[0]),
        )
    assert restarted.compress_cold_payloads(subject, limit=1) == 1


def test_compaction_failure_is_durable_and_does_not_block_cleanup(tmp_path: Any) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-compaction-failure"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    ProviderHealthStore(db).record_attempt(subject, "model", "provider", "old", True, 1, None)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE provider_health_buckets SET bucket_start=?",
            ((datetime.now(UTC) - timedelta(days=60)).isoformat(),),
        )
    manager = RetentionManager(db)
    with patch.object(ModelLedger, "compress_cold_payloads", side_effect=RuntimeError("injected")):
        result = manager.run_batch(subject)
    assert result["failure_stage"] == "compaction"
    assert result["deleted_by_table"]["provider_health_buckets"] == 1
    with db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM retention_runs WHERE run_id=?", (result["run_id"],)
        ).fetchone()
    validate_retention_run_row(row)
    assert row["failure_count"] == 1
    assert row["retry_at"]


def test_pressure_retention_skips_compaction(tmp_path: Any) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-compaction-pressure"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    with patch.object(ModelLedger, "compress_cold_payloads") as compact:
        result = RetentionManager(db, minimum_free_bytes=2**63).run_batch(subject)
    compact.assert_not_called()
    assert result["compaction_skipped"] is True
    assert result["failed_reason"] is None


def test_run_batch_is_bounded_and_removes_old_health_rows(tmp_path: Any) -> None:
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


def test_run_batch_prunes_only_expired_search_rate_limit_records(tmp_path: Any) -> None:
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


def test_retention_next_cursor_is_per_table_and_resumes_each_table(tmp_path: Any) -> None:
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


def test_retention_failure_is_persisted_for_diagnostics(tmp_path: Any) -> None:
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


def test_retention_resumes_from_previous_keyset_cursor(tmp_path: Any) -> Any:
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
    stable_now = datetime.now(UTC).replace(second=0, microsecond=0)

    def wrapped(
        connection: Any, table: Any, subject_id: Any, cutoff: Any, limit: Any, cursor: Any
    ) -> Any:
        if table == "provider_health_buckets":
            observed.append(cursor)
        return original(connection, table, subject_id, cutoff, limit, cursor)

    with patch.object(manager, "_delete_table", side_effect=wrapped):
        assert manager.run_batch(subject, now=stable_now, batch_size=1)["failed_reason"] is None
        assert manager.run_batch(subject, now=stable_now, batch_size=1)["failed_reason"] is None

    assert observed[0] is None
    assert isinstance(observed[1], dict)
    assert observed[1]["bucket_start"] == old


def test_retention_resets_cursor_when_cutoff_advances_and_new_old_row_precedes_cursor(
    tmp_path: Any,
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention-cutoff-reset"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    health = ProviderHealthStore(db, failure_threshold=1)
    for provider in ("a", "b"):
        health.record_attempt(subject, "model", provider, f"attempt-{provider}", True, 10, None)
    old_bucket = "2025-01-01T00:00:00+00:00"
    with db.transaction() as connection:
        connection.execute(
            "UPDATE provider_health_buckets SET bucket_start=?, state_hash=? WHERE subject_id=?",
            (old_bucket, content_hash({"legacy": True}), subject),
        )

    manager = RetentionManager(db, RetentionSettings(batch_size=1, health_days=30))
    first_now = datetime(2026, 1, 1, tzinfo=UTC)
    second_now = first_now + timedelta(days=1)
    first = manager.run_batch(subject, now=first_now, batch_size=1)
    assert first["failed_reason"] is None

    health.record_attempt(subject, "model", "0", "attempt-0", True, 10, None)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE provider_health_buckets SET bucket_start=?, state_hash=? "
            "WHERE subject_id=? AND provider_id=?",
            (old_bucket, content_hash({"legacy": True, "provider": "0"}), subject, "0"),
        )

    second = manager.run_batch(subject, now=second_now, batch_size=1)
    assert second["failed_reason"] is None
    with db.connection() as connection:
        remaining = connection.execute(
            "SELECT provider_id FROM provider_health_buckets WHERE subject_id=? "
            "ORDER BY provider_id",
            (subject,),
        ).fetchall()
    assert [row["provider_id"] for row in remaining] == ["b"]


def test_retention_never_schedules_append_only_route_history(tmp_path: Any) -> Any:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention-route-history"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    manager = RetentionManager(db)
    calls: list[str] = []
    original = manager._delete_table

    def wrapped(
        connection: Any, table: Any, subject_id: Any, cutoff: Any, limit: Any, cursor: Any
    ) -> Any:
        calls.append(table)
        return original(connection, table, subject_id, cutoff, limit, cursor)

    with patch.object(manager, "_delete_table", side_effect=wrapped):
        result = manager.run_batch(subject)
    assert result["failed_reason"] is None
    assert "cognitive_route_attempts" not in calls
    assert "cognitive_route_outcomes" not in calls


def test_long_lived_tables_have_explicit_retention_classification() -> None:
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


def test_append_only_evidence_is_never_in_delete_registry() -> None:
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


def test_retention_projection_uses_registry_cutoff_metadata(tmp_path: Any) -> None:
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


def test_retention_registry_reports_runtime_table_added_after_schema_baseline(
    tmp_path: Any,
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    with db.transaction() as connection:
        connection.execute(
            "CREATE TABLE unregistered_history(subject_id TEXT NOT NULL, entry_id TEXT PRIMARY KEY)"
        )
        diagnostics = retention_registry_diagnostics(connection)
    assert "unregistered_history" in diagnostics["unclassified"]


def test_retention_run_return_and_persisted_hash_match(tmp_path: Any) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    subject = "Noyra-retention-hash"
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    result = RetentionManager(db, RetentionSettings(run_history=2)).run_batch(subject)
    with db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM retention_runs WHERE run_id=?", (result["run_id"],)
        ).fetchone()
    validate_retention_run_row(row)
    assert result["protected_rows"] == row["protected_rows"]
    assert result["deleted_by_table"] == json.loads(row["deleted_by_table_json"])
