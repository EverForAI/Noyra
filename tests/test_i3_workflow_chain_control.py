from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core.admission import OperationInvalidated
from noyra.core.archive import ArchiveKeyring
from noyra.core.errors import ArchiveKeyUnavailableError, IntegrityError, RuntimeOwnershipError
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import canonical_json
from noyra.core.workflow_bundle import _history
from noyra.core.workflow_chain_control import open_workflow_chain
from noyra.core.workflow_export_files import WorkflowArtifactStore
from noyra.core.workflow_increment_bundle import artifact_store
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
from noyra.core.workflow_increment_files import WorkflowIncrementStore
from test_d2b_workflow_export_control import control as control
from test_i2_workflow_increment_control import advance, publish
from test_i2_workflow_increment_control import context as context


@pytest.fixture
def chain(
    context: tuple[OperatorControlService, str],
) -> tuple[OperatorControlService, str, str, str]:
    controls, base = context
    with patch.object(WorkflowIncrementStore, "flush", return_value=True):
        first = publish(context)
        advance(controls)
        second = publish(context, parent=first["publication_id"], version=8, key="chain-second-key")
    return controls, base, first["publication_id"], second["publication_id"]


def test_chain_records_are_complete_original_query_only_and_context_bound(chain: Any) -> None:
    controls, base, first, tip = chain
    with controls.database.read_transaction() as c:
        expected = list(
            _history(c, controls.database, controls.subject_id, "export-workflow", lambda: None)
        )
    with (
        patch.object(controls.database, "transaction", side_effect=AssertionError("source write")),
        patch.object(WorkflowArtifactStore, "put", side_effect=AssertionError("archive write")),
        patch.object(
            WorkflowArtifactStore, "flush", side_effect=AssertionError("durability mutation")
        ),
        open_workflow_chain(controls, tip) as view,
    ):
        result = view.summary()
        assert result["tip_id"] == tip and result["base_id"] == base
        assert result["records"] == 8 and result["ancestor_boundaries_verified"] == 3
        assert result["chain_depth"] == 2 and result["chain_nodes"] == 3
        assert result["complete_history_verified"] and result["isolated_reconstruction_verified"]
        assert result["restore_or_dispatch_authorized"] is False
        assert canonical_json(view.page()["records"]) == canonical_json(expected)
        page = view.page(limit=3)
        assert page["next_after_version"] == 3 and page["truncated"]
        assert len(view.page(after_version=3)["records"]) == 5
        assert view.latest()["row"]["checkpoint_version"] == 8
        assert view.page(after_version=8)["records"] == []
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            view._connection.execute("DELETE FROM reconstructed_records")
        assert view._connection.execute("PRAGMA database_list").fetchone()[2] == ""
        for after, limit in [(True, 1), (-1, 1), (9, 1), (0, False), (0, 101)]:
            with pytest.raises(ValueError):
                view.page(after_version=after, limit=limit)
    with pytest.raises(ValueError, match="closed"):
        view.page()
    assert controls.kernel.admission.active_operations == 0
    assert controls.kernel.lifecycle.current().state == "paused"
    with open_workflow_chain(controls, first) as earlier:
        assert earlier.summary()["records"] == 6
    with open_workflow_chain(controls, base) as original:
        assert original.summary()["records"] == 4 and original.summary()["chain_depth"] == 0


def test_selected_tip_does_not_follow_later_live_progress_or_sibling_branch(chain: Any) -> None:
    controls, base, first, tip = chain
    advance(controls, 1)
    with patch.object(WorkflowIncrementStore, "flush", return_value=True):
        sibling = publish((controls, base), version=9, key="sibling-branch-key")
    for selected, count in [(first, 6), (tip, 8), (sibling["publication_id"], 9)]:
        with open_workflow_chain(controls, selected) as view:
            assert view.summary()["tip_id"] == selected and view.summary()["records"] == count


@pytest.mark.parametrize("state", ["prepared", "published_durability_unknown"])
def test_unaccepted_tip_never_authorizes_read_or_promotion(context: Any, state: str) -> None:
    controls, _ = context
    if state == "prepared":
        with (
            patch.object(WorkflowIncrementStore, "put", side_effect=OSError),
            pytest.raises(OSError),
        ):
            publish(context)
    else:
        with patch.object(WorkflowIncrementStore, "flush", return_value=False):
            publish(context)
    item = controls.workflow_increments()["items"][0]
    with pytest.raises(IntegrityError, match="not accepted"):
        controls.verify_workflow_chain(item["publication_id"])
    assert controls.workflow_increments()["items"][0]["status"] == state
    assert controls.kernel.admission.active_operations == 0


@pytest.mark.parametrize("fence", ["active", "quarantine", "migration", "unowned", "drain"])
def test_read_obeys_all_runtime_admission_fences(chain: Any, fence: str) -> None:
    controls, _base, _first, tip = chain
    if fence == "active":
        controls.kernel.resume("Synthetic active runtime")
    elif fence == "quarantine":
        controls.kernel.admission.quarantine()
    elif fence == "migration":
        with controls.kernel.admission.migration_control_scope():
            controls.kernel.admission.fence_for_migration(timeout=0)
    elif fence == "unowned":
        controls.kernel.process_lock.release()
    else:
        with (
            controls.kernel.admission.operation("other", allow_quarantine=True),
            pytest.raises(OperationInvalidated),
        ):
            controls.verify_workflow_chain(tip)
        return
    with pytest.raises((OperationInvalidated, RuntimeOwnershipError)):
        controls.verify_workflow_chain(tip)


@pytest.mark.parametrize("location", ["tip", "parent", "base"])
def test_missing_or_corrupt_accepted_dependency_never_repairs_or_exposes_partial_view(
    chain: Any,
    location: str,
) -> None:
    from noyra.core.at_rest import AtRestError
    from noyra.core.workflow_export_catalog import WorkflowExportCatalog

    controls, base, first, tip = chain
    selected = {"tip": tip, "parent": first, "base": base}[location]
    with controls.database.read_transaction() as c:
        catalog = WorkflowIncrementCatalog(controls.database, controls.subject_id)
        record = (
            WorkflowExportCatalog(controls.database, controls.subject_id).get(c, selected)
            if location == "base"
            else catalog.get(c, selected)
        )
    assert record is not None
    files = artifact_store(
        controls.database, record, ArchiveKeyring.from_env(), base=location == "base"
    )
    path = files.root / (selected + "_segment-000000.enc")
    if location == "parent":
        path.unlink()
    else:
        path.write_bytes(b"synthetic damaged encrypted dependency")
    with (
        patch.object(WorkflowArtifactStore, "put", side_effect=AssertionError("repair")),
        pytest.raises((IntegrityError, AtRestError)),
        open_workflow_chain(controls, tip),
    ):
        pytest.fail("partial view escaped")
    assert controls.kernel.admission.active_operations == 0


@pytest.mark.parametrize("change", ["epoch", "privacy", "key", "at_rest", "clock"])
def test_open_view_access_rechecks_revocation_before_exposing_more_records(
    chain: Any, change: str
) -> None:
    from noyra.interaction.conversations import ConversationStore

    controls, _base, _first, tip = chain
    window = ConversationStore(controls.database).create(
        controls.subject_id, "Synthetic private window"
    )
    with open_workflow_chain(controls, tip) as view:
        assert len(view.page(limit=1)["records"]) == 1
        if change == "epoch":
            controls.kernel.admission.invalidate()
            with pytest.raises(OperationInvalidated):
                view.page(after_version=1)
        elif change == "privacy":
            with controls.database.transaction() as c:
                c.execute(
                    "UPDATE private_conversations SET status='deleting' WHERE conversation_id=?",
                    (window["conversation_id"],),
                )
            with pytest.raises(OperationInvalidated, match="privacy"):
                view.page(after_version=1)
        elif change == "key":
            with (
                patch(
                    "noyra.core.workflow_chain_control.ArchiveKeyring.from_env",
                    side_effect=ArchiveKeyUnavailableError("fixture"),
                ),
                pytest.raises(ArchiveKeyUnavailableError),
            ):
                view.latest()
        elif change == "at_rest":
            from types import SimpleNamespace

            controls.at_rest = SimpleNamespace(
                required=True,
                require_ready=lambda: (_ for _ in ()).throw(PermissionError("storage closed")),
            )
            with pytest.raises(PermissionError):
                view.summary()
        else:
            with (
                patch("noyra.core.workflow_chain_control.time.monotonic", return_value=10**12),
                pytest.raises(TimeoutError),
            ):
                view.page()
    assert controls.kernel.admission.active_operations == 0


def test_privacy_change_mid_page_is_rechecked_before_return(chain: Any) -> None:
    from noyra.interaction.conversations import ConversationStore

    controls, _base, _first, tip = chain
    window = ConversationStore(controls.database).create(
        controls.subject_id, "Synthetic private window"
    )
    with open_workflow_chain(controls, tip) as view:
        original = view._checkpoint
        calls = 0

        def withdraw() -> None:
            nonlocal calls
            original()
            calls += 1
            if calls == 2:
                with controls.database.transaction() as c:
                    c.execute(
                        "UPDATE private_conversations SET status='deleting' "
                        "WHERE conversation_id=?",
                        (window["conversation_id"],),
                    )

        view._checkpoint = withdraw
        with pytest.raises(OperationInvalidated, match="privacy"):
            view.page()


def test_invalid_or_foreign_tip_cannot_select_paths_or_another_subject(chain: Any) -> None:
    from types import SimpleNamespace

    controls, _base, _first, tip = chain
    for selected in ["../manifest.enc", "latest", "wdelta_" + "0" * 64, "", tip + "/extra"]:
        with pytest.raises((ValueError, IntegrityError)):
            controls.verify_workflow_chain(selected)
    foreign = OperatorControlService(
        SimpleNamespace(database=controls.database, subject_id="foreign")
    )
    # Scope lookup itself is subject-bound; runtime-free catalog cannot locate it.
    with controls.database.read_transaction() as c:
        assert WorkflowIncrementCatalog(foreign.database, foreign.subject_id).get(c, tip) is None


def test_read_uses_no_source_writer_and_does_not_open_persistent_reconstruction(chain: Any) -> None:
    controls, _base, _first, tip = chain
    original = WorkflowArtifactStore.get
    count = 0

    def reading(self: WorkflowArtifactStore, item: Any, *, maximum: int) -> bytes:
        nonlocal count
        with closing(controls.database._connect()) as c:
            c.execute("PRAGMA busy_timeout=0")
            c.execute("BEGIN IMMEDIATE")
            c.rollback()
            count += 1
        return original(self, item, maximum=maximum)

    before = set(controls.database.path.parent.iterdir())
    with (
        patch.object(WorkflowArtifactStore, "get", reading),
        open_workflow_chain(controls, tip) as view,
    ):
        assert view._connection.execute("PRAGMA database_list").fetchone()[2] == ""
        assert view.summary()["storage"] == "ephemeral_memory_sqlite"
    assert set(controls.database.path.parent.iterdir()) == before and count >= 6
