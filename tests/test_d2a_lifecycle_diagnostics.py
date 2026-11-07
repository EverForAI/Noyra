from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.lifecycle_diagnostics import LifecycleCatalogUnavailable, lifecycle_catalog
from noyra.core.lifecycle_inventory import TABLE_DOMAINS
from noyra.core.retention import RETENTION_TABLES, RetentionManager, retention_registry_diagnostics
from noyra.core.types import canonical_json
from test_d1_workflow_queries import workflows as workflows


def test_catalog_does_not_confuse_refreshed_schema_baseline_with_policy_review(
    workflows: DurableWorkflowStore,
) -> None:
    manager = RetentionManager(workflows.database)
    with workflows.database.transaction() as c:
        c.execute(
            "CREATE TABLE synthetic_unreviewed_history(id TEXT PRIMARY KEY, private_payload TEXT)"
        )
        names = sorted(
            row[0]
            for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] != "sqlite_sequence"
        )
        c.execute(
            "UPDATE schema_meta SET value=? WHERE key='retention_contract_inventory'",
            (canonical_json(names),),
        )
        assert retention_registry_diagnostics(c)["unclassified"] == ()
    catalog = manager.lifecycle_catalog(limit=100)
    assert catalog["policy_coverage"]["unreviewed_objects"] == 1
    assert catalog["policy_coverage"]["status"] == "review_required"
    assert catalog["structural_drift"]["unclassified"] == []
    assert RETENTION_TABLES == (
        "provider_health_buckets",
        "provider_health_attempts",
        "search_provider_uses",
        "retention_runs",
    )


def pages(manager: RetentionManager, *, limit: int = 17) -> list[dict[str, Any]]:
    result = []
    cursor = None
    while True:
        page = manager.lifecycle_catalog(limit=limit, cursor=cursor)
        assert len(page["items"]) <= limit
        assert page["page"]["returned"] == len(page["items"])
        result.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            assert not page["page"]["has_more"]
            return result


def test_current_preservation_baseline_is_explicit_complete_and_read_only(
    workflows: DurableWorkflowStore,
) -> None:
    manager = RetentionManager(workflows.database)
    result = manager.lifecycle_catalog(limit=100)
    coverage = result["policy_coverage"]
    assert coverage["status"] == "known_preservation_inventory"
    assert coverage["advisory_only"] and not coverage["new_cleanup_authorized"]
    items = pages(manager)
    names = [row["table"] for row in items]
    assert names == sorted(TABLE_DOMAINS) and len(names) == len(set(names))
    assert coverage["objects"] == len(names)
    assert sum(
        coverage[key]
        for key in (
            "implemented_retention_objects",
            "documented_domain_objects",
            "preservation_only_objects",
            "unreviewed_objects",
        )
    ) == len(names)
    rows = {item["table"]: item for item in items}
    assert rows["memory_fts_data"]["representation_contract"] == "virtual_table_owned_shadow"
    assert rows["workflow_checkpoints"]["policy_state"] == "preservation_baseline"
    assert rows["participant_sessions"]["existing_domain_contract"] == "participant_session_expiry"
    assert rows["retention_runs"]["existing_retention_action"] == "delete"
    assert all(
        item["default_action"] == "preserve" and not item["new_cleanup_authorized"]
        for item in items
    )


def test_unknown_names_do_not_inherit_review_and_structure_drift_stays_distinct(
    workflows: DurableWorkflowStore,
) -> None:
    manager = RetentionManager(workflows.database)
    with workflows.database.transaction() as c:
        c.execute("CREATE TABLE wallet_synthetic_unreviewed(id TEXT, secret TEXT)")
        c.execute(
            "INSERT INTO wallet_synthetic_unreviewed VALUES('synthetic','NEVER_RETURN_CONTENT')"
        )
    result = manager.lifecycle_catalog(limit=100)
    assert result["structural_drift"]["status"] == "mismatch"
    assert result["policy_coverage"]["status"] == "review_required"
    row = next(item for item in pages(manager) if item["table"] == "wallet_synthetic_unreviewed")
    assert row["policy_state"] == "unreviewed_preserve" and row["domain"] is None
    assert row["existing_retention_action"] is None and not row["new_cleanup_authorized"]
    assert "NEVER_RETURN_CONTENT" not in str(result)
    # Authorizer refuses any data reads beyond schema metadata.
    import sqlite3

    with workflows.database.read_transaction() as c:

        def allow_metadata(action: int, table: str | None, *_: object) -> int:
            if action == sqlite3.SQLITE_READ and table not in {"sqlite_master", "schema_meta"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        c.set_authorizer(allow_metadata)
        observed = lifecycle_catalog(c, limit=100)
        assert observed["policy_coverage"]["unreviewed_objects"] == 1
        assert c.total_changes == 0


@pytest.mark.parametrize("change", ["ddl", "baseline"])
def test_pagination_rejects_a_changed_schema_or_baseline(
    workflows: DurableWorkflowStore,
    change: str,
) -> None:
    manager = RetentionManager(workflows.database)
    first = manager.lifecycle_catalog(limit=5)
    with workflows.database.transaction() as c:
        if change == "ddl":
            c.execute("CREATE TABLE synthetic_new_schema(id INTEGER)")
        else:
            c.execute("UPDATE schema_meta SET value='[]' WHERE key='retention_contract_inventory'")
    with pytest.raises(ValueError, match="cursor"):
        manager.lifecycle_catalog(limit=5, cursor=first["next_cursor"])


@pytest.mark.parametrize("value", [None, "broken", "{}", '["events","events"]', "[true]"])
def test_missing_or_invalid_schema_baseline_is_not_reported_clean(
    workflows: DurableWorkflowStore,
    value: str | None,
) -> None:
    with workflows.database.transaction() as c:
        if value is None:
            c.execute("DELETE FROM schema_meta WHERE key='retention_contract_inventory'")
        else:
            c.execute(
                "UPDATE schema_meta SET value=? WHERE key='retention_contract_inventory'", (value,)
            )
    result = RetentionManager(workflows.database).lifecycle_catalog()
    assert result["structural_drift"]["status"] == "baseline_unavailable"
    assert result["structural_drift"]["unclassified_count"] > 0
    assert result["structural_drift"]["truncated"]
    assert result["policy_coverage"]["unreviewed_objects"] == 0


@pytest.mark.parametrize("limit", [True, False, 0, 101, -1])
def test_lifecycle_page_limit_is_strict(workflows: DurableWorkflowStore, limit: int) -> None:
    with pytest.raises(ValueError):
        RetentionManager(workflows.database).lifecycle_catalog(limit=limit)


def test_metadata_bound_reports_unavailable_without_truncated_success(
    workflows: DurableWorkflowStore,
) -> None:
    with (
        patch("noyra.core.lifecycle_diagnostics.MAX_OBJECTS", 10),
        pytest.raises(LifecycleCatalogUnavailable),
    ):
        RetentionManager(workflows.database).lifecycle_catalog()


def test_long_or_hostile_cursor_cannot_be_used_as_a_query(workflows: DurableWorkflowStore) -> None:
    manager = RetentionManager(workflows.database)
    for cursor in ("x" * 2049, "' OR 1=1 --", "", "%%%", "bm9uLWpzb24="):
        with pytest.raises(ValueError):
            manager.lifecycle_catalog(cursor=cursor)


def test_oversized_schema_baseline_is_unavailable_not_a_claim_of_coverage(
    workflows: DurableWorkflowStore,
) -> None:
    with workflows.database.transaction() as c:
        c.execute(
            "UPDATE schema_meta SET value=? WHERE key='retention_contract_inventory'",
            (" " * 600_000,),
        )
    result = RetentionManager(workflows.database).lifecycle_catalog()
    assert result["structural_drift"]["status"] == "baseline_unavailable"
    assert result["structural_drift"]["baseline_status"] == "invalid"


def test_policy_gap_is_advisory_without_waiving_original_structure_failures(
    workflows: DurableWorkflowStore,
) -> None:
    from noyra.core.integrity import IntegrityRegistry

    with workflows.database.transaction() as c:
        c.execute("CREATE TABLE synthetic_unreviewed_note(id TEXT)")
    registry = IntegrityRegistry()
    mismatched = registry.run(
        workflows.database,
        "one",
        workflows.database.path.parent,
        profile="manual",
        policy_mode="pause",
        deadline_seconds=5,
        check_ids=("operations.retention",),
    )
    assert mismatched.findings
    with workflows.database.transaction() as c:
        names = sorted(
            row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
        c.execute(
            "UPDATE schema_meta SET value=? WHERE key='retention_contract_inventory'",
            (canonical_json(names),),
        )
    observed = RetentionManager(workflows.database).lifecycle_catalog()
    assert observed["policy_coverage"]["unreviewed_objects"] == 1
    reviewed_structure = registry.run(
        workflows.database,
        "one",
        workflows.database.path.parent,
        profile="manual",
        policy_mode="pause",
        deadline_seconds=5,
        check_ids=("operations.retention",),
    )
    assert not reviewed_structure.findings


def test_observation_time_budget_reports_unavailable(workflows: DurableWorkflowStore) -> None:
    with (
        patch("noyra.core.retention.time.monotonic", side_effect=[0.0, 1.0]),
        pytest.raises(LifecycleCatalogUnavailable),
    ):
        RetentionManager(workflows.database).lifecycle_catalog()


def test_baseline_missing_object_is_not_hidden_by_known_preservation_inventory(
    workflows: DurableWorkflowStore,
) -> None:
    with workflows.database.transaction() as c:
        row = c.execute(
            "SELECT value FROM schema_meta WHERE key='retention_contract_inventory'"
        ).fetchone()
        import json

        names = json.loads(row[0])
        names.append("synthetic_missing_object")
        c.execute(
            "UPDATE schema_meta SET value=? WHERE key='retention_contract_inventory'",
            (canonical_json(names),),
        )
    result = RetentionManager(workflows.database).lifecycle_catalog()
    assert result["policy_coverage"]["unreviewed_objects"] == 0
    assert result["structural_drift"]["status"] == "mismatch"
    assert result["structural_drift"]["missing"] == ["synthetic_missing_object"]
