from __future__ import annotations

import os
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.admission import OperationInvalidated
from noyra.core.archive import ArchiveKeyring
from noyra.core.errors import IntegrityError, RuntimeOwnershipError
from noyra.core.operator_controls import OperatorControlService
from noyra.core.scheduling_clock import aware_time
from noyra.core.types import canonical_json, content_hash
from noyra.core.workflow_bundle import _history, inspect_source
from noyra.core.workflow_export_files import WorkflowArtifactStore
from noyra.core.workflow_increment_bundle import (
    _chain_records,
    accepted_chain,
    artifact_store,
    verify_increment_chain,
)
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
from noyra.core.workflow_increment_files import WorkflowIncrementStore
from test_d2b_workflow_export_control import control as control
from test_d2b_workflow_export_control import run as base_export


def advance(control: OperatorControlService, count: int = 2) -> None:
    for index in range(count):
        with control.database.read_transaction() as c:
            when = c.execute(
                "SELECT created_at FROM workflow_checkpoints "
                "ORDER BY checkpoint_version DESC LIMIT 1"
            ).fetchone()[0]
        observed = aware_time(when) + timedelta(hours=-1 if index % 2 == 0 else 1)
        with patch("noyra.autonomy.workflow.utc_now", return_value=observed.isoformat()):
            DurableWorkflowStore(control.database).checkpoint(
                control.subject_id,
                "export-workflow",
                "fixture",
                "running",
                {"suffix": index},
                reason="SYNTHETIC_PRIVATE_INCREMENT_REASON",
            )


@pytest.fixture
def context(control: OperatorControlService) -> tuple[OperatorControlService, str]:
    # Actual file encoding; Windows flush is simulated only to construct an
    # independently accepted parent. Actual POSIX fsync is tested separately.
    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        base = base_export(control)
    advance(control)
    return control, base["publication_id"]


def publish(
    context: tuple[OperatorControlService, str],
    *,
    parent: str | None = None,
    version: int = 6,
    key: str = "increment-original-request",
) -> dict[str, Any]:
    controls, base = context
    return controls.export_workflow_increment(
        "export-workflow",
        parent or base,
        version,
        key,
        actor="operator",
        reason="Synthetic reviewed incremental export",
    )


def test_actual_encoding_and_complete_readback_preserve_original_evidence(
    context: tuple[OperatorControlService, str],
) -> None:
    controls, base = context
    with controls.database.read_transaction() as c:
        before = [dict(row) for row in c.execute("SELECT * FROM workflow_checkpoints")]
    result = publish(context)
    assert result["status"] == ("published_durability_unknown" if os.name == "nt" else "accepted")
    assert result["readback_verified"] and result["durability_confirmed"] == (os.name != "nt")
    assert result["increment_records"] == 2 and result["records"] == 6
    assert result["parent_id"] == base and result["restore_or_dispatch_authorized"] is False
    ring = ArchiveKeyring.from_env()
    catalog = WorkflowIncrementCatalog(controls.database, controls.subject_id)
    with controls.database.read_transaction() as c:
        record = catalog.get(c, result["publication_id"])
        nodes = accepted_chain(c, catalog, base, lambda: None)
        assert [dict(row) for row in c.execute("SELECT * FROM workflow_checkpoints")] == before
        expected = list(
            _history(c, controls.database, controls.subject_id, "export-workflow", lambda: None)
        )
        assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    assert record is not None
    nodes.append(("increment", record))
    verify_increment_chain(controls.database, nodes, ring, lambda: None)
    actual = list(_chain_records(controls.database, nodes, ring, lambda: None))
    assert canonical_json(actual) == canonical_json(expected)
    assert actual[4]["receipt"]["payload"]["mode"] == "rollback"
    files = artifact_store(controls.database, record, ring, base=False)
    assert files.root.parent.name == "workflow-increments"
    assert all(b"SYNTHETIC_PRIVATE" not in path.read_bytes() for path in files.root.iterdir())
    assert controls.kernel.lifecycle.current().state == "paused"
    assert controls.kernel.admission.active_operations == 0
    assert (
        controls.workflow_increments()["items"][0]["artifact_observation"]
        == "not_checked_by_catalog_read"
    )


def test_accepted_request_retries_do_not_retarget_later_source_or_rewrite_files(
    context: tuple[OperatorControlService, str],
) -> None:
    controls, _ = context
    with patch.object(WorkflowIncrementStore, "flush", return_value=True):
        result = publish(context)
        same_source = publish(context, key="another-request-key")
    assert same_source["publication_id"] == result["publication_id"] and same_source["idempotent"]
    advance(controls, 1)
    with patch.object(
        WorkflowIncrementStore, "put", side_effect=AssertionError("accepted rewrite")
    ):
        repeated = publish(context)
    assert repeated["publication_id"] == result["publication_id"] and repeated["idempotent"]
    with pytest.raises(ValueError, match="another intent"):
        publish(context, version=7)
    with controls.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM workflow_increment_publications").fetchone()[0] == 1
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='workflow_increment_accepted'"
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM audit_records WHERE action='workflow_increment_accepted'"
            ).fetchone()[0]
            == 1
        )


def test_two_levels_of_real_increment_objects_roundtrip(
    context: tuple[OperatorControlService, str],
) -> None:
    controls, _ = context
    with patch.object(WorkflowIncrementStore, "flush", return_value=True):
        first = publish(context)
        advance(controls)
        second = publish(
            context, parent=first["publication_id"], version=8, key="second-increment-key"
        )
    assert (
        second["chain_depth"] == 2
        and second["first_version"] == 7
        and second["increment_records"] == 2
    )
    with controls.database.read_transaction() as c:
        catalog = WorkflowIncrementCatalog(controls.database, controls.subject_id)
        nodes = accepted_chain(c, catalog, second["publication_id"], lambda: None)
        original = list(
            _history(c, controls.database, controls.subject_id, "export-workflow", lambda: None)
        )
    ring = ArchiveKeyring.from_env()
    verify_increment_chain(controls.database, nodes, ring, lambda: None)
    assert canonical_json(
        list(_chain_records(controls.database, nodes, ring, lambda: None))
    ) == canonical_json(original)


@pytest.mark.parametrize("fence", ["active", "unowned", "quarantine", "migration", "drain"])
def test_invalid_runtime_rejected_before_increment_files_or_preparation(
    context: tuple[OperatorControlService, str],
    fence: str,
) -> None:
    controls, _ = context
    if fence == "active":
        controls.kernel.resume("Synthetic active")
    elif fence == "unowned":
        controls.kernel.process_lock.release()
    elif fence == "quarantine":
        controls.kernel.admission.quarantine()
    elif fence == "migration":
        with controls.kernel.admission.migration_control_scope():
            controls.kernel.admission.fence_for_migration(timeout=0)
    else:
        with (
            controls.kernel.admission.operation("other", allow_quarantine=True),
            pytest.raises(OperationInvalidated),
        ):
            publish(context)
        return
    with pytest.raises((OperationInvalidated, RuntimeOwnershipError)):
        publish(context)
    with controls.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM workflow_increment_publications").fetchone()[0] == 0
    assert not list((controls.database.path.parent / "subject").rglob("workflow-increments"))


def test_rehashed_old_prefix_is_rejected_while_latest_parent_row_is_unchanged(
    context: tuple[OperatorControlService, str],
) -> None:
    controls, _ = context
    with controls.database.transaction() as c:
        c.execute(
            "UPDATE workflow_checkpoints SET state_json=?,state_hash=? WHERE checkpoint_version=1",
            (canonical_json({"changed": True}), content_hash({"changed": True})),
        )
    with controls.database.read_transaction() as c:
        assert (
            inspect_source(
                c, controls.database, controls.subject_id, "export-workflow", lambda: None
            )["records"]
            == 6
        )
    with pytest.raises(IntegrityError, match="differs"):
        publish(context)
    assert not controls.workflow_increments()["items"]


@pytest.mark.parametrize("stage", ["segment", "manifest", "audit"])
def test_interrupted_publication_retains_prepared_evidence_then_original_request_recovers(
    context: tuple[OperatorControlService, str],
    stage: str,
) -> None:
    controls, _ = context
    original = WorkflowIncrementStore.put

    def interrupted(self: WorkflowIncrementStore, name: str, payload: bytes) -> dict[str, Any]:
        result = original(self, name, payload)
        if (stage == "manifest" and name == "manifest.enc") or (
            stage == "segment" and name.startswith("segment")
        ):
            raise OSError("synthetic object boundary")
        return result

    with patch.object(WorkflowIncrementStore, "flush", return_value=True):
        with pytest.raises(OSError):
            if stage == "audit":
                with patch.object(
                    controls, "_audit_connection", side_effect=OSError("synthetic audit")
                ):
                    publish(context)
            else:
                with patch.object(WorkflowIncrementStore, "put", interrupted):
                    publish(context)
        assert controls.workflow_increments()["items"][0]["status"] == "prepared"
        with controls.database.read_transaction() as c:
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type='workflow_increment_accepted'"
                ).fetchone()[0]
                == 0
            )
        assert publish(context)["status"] == "accepted"
    assert controls.kernel.admission.active_operations == 0


def test_unconfirmed_promotion_needs_unchanged_source_and_never_rewrites_published_files(
    context: tuple[OperatorControlService, str],
) -> None:
    with patch.object(WorkflowIncrementStore, "flush", return_value=False):
        unknown = publish(context)
    assert unknown["status"] == "published_durability_unknown"
    with pytest.raises(IntegrityError, match="not accepted"):
        publish(context, parent=unknown["publication_id"], key="unconfirmed-parent-key")
    with (
        patch.object(WorkflowIncrementStore, "flush", return_value=True),
        patch.object(
            WorkflowIncrementStore, "put", side_effect=AssertionError("published rewrite")
        ),
    ):
        assert publish(context)["status"] == "accepted"


def test_source_writer_during_file_io_never_commits_acceptance(
    context: tuple[OperatorControlService, str],
) -> None:
    from noyra.core import EventStore

    controls, _ = context
    original = WorkflowIncrementStore.put

    def changing(self: WorkflowIncrementStore, name: str, payload: bytes) -> dict[str, Any]:
        result = original(self, name, payload)
        if name == "manifest.enc":
            EventStore(controls.database).append(controls.subject_id, "unrelated", "fixture", {})
        return result

    with (
        patch.object(WorkflowIncrementStore, "put", changing),
        pytest.raises(ValueError, match="database changed"),
    ):
        publish(context)
    assert controls.workflow_increments()["items"][0]["status"] == "prepared"
    assert publish(context)["readback_verified"]


def test_invalidated_lease_during_write_keeps_prepared_and_releases_operation(
    context: tuple[OperatorControlService, str],
) -> None:
    controls, _ = context
    original = WorkflowIncrementStore.put

    def invalidating(self: WorkflowIncrementStore, name: str, payload: bytes) -> dict[str, Any]:
        result = original(self, name, payload)
        controls.kernel.admission.invalidate()
        return result

    with (
        patch.object(WorkflowIncrementStore, "put", invalidating),
        pytest.raises(OperationInvalidated),
    ):
        publish(context)
    assert controls.kernel.admission.active_operations == 0
    assert controls.workflow_increments()["items"][0]["status"] == "prepared"
