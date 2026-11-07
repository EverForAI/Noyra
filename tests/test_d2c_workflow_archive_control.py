from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core.admission import OperationInvalidated
from noyra.core.archive import ArchiveKeyring
from noyra.core.errors import ArchiveKeyUnavailableError, IntegrityError, RuntimeOwnershipError
from noyra.core.operator_controls import OperatorControlService
from noyra.core.workflow_archive_control import open_workflow_export
from noyra.core.workflow_export_catalog import WorkflowExportCatalog
from noyra.core.workflow_export_files import WorkflowArtifactStore
from test_d2b_workflow_export_control import control as control
from test_d2b_workflow_export_control import run


@pytest.fixture
def publication(control: OperatorControlService) -> str:
    # This proves reader behavior, not Windows directory durability.
    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        return str(run(control)["publication_id"])


def test_reader_is_complete_readonly_source_bound_and_context_limited(
    control: OperatorControlService, publication: str
) -> None:
    with (
        patch.object(control.database, "transaction", side_effect=AssertionError("source write")),
        patch.object(WorkflowArtifactStore, "put", side_effect=AssertionError("artifact write")),
        patch.object(
            WorkflowArtifactStore, "flush", side_effect=AssertionError("durability change")
        ),
        open_workflow_export(control, publication) as view,
    ):
        assert view.latest()["row"]["checkpoint_version"] == 4
        assert view.page(limit=2)["next_after_version"] == 2
        assert view.summary()["isolated_reconstruction_verified"]
        assert view.summary()["restore_or_dispatch_authorized"] is False
    assert control.kernel.admission.active_operations == 0
    assert control.kernel.lifecycle.current().state == "paused"
    with pytest.raises(ValueError, match="closed"):
        view.page()
    summary = control.verify_workflow_export(publication)
    assert summary["publication_id"] == publication and summary["records"] == 4
    assert "Synthetic private state" not in str(summary)
    assert "state_json" not in str(summary) and "manifest" not in summary


def test_accepted_reader_does_not_require_original_live_workflow_to_stay_frozen(
    control: OperatorControlService, publication: str
) -> None:
    from noyra.autonomy.workflow import DurableWorkflowStore

    DurableWorkflowStore(control.database).checkpoint(
        control.subject_id, "export-workflow", "fixture", "running", {"n": 4}, reason="New work"
    )
    with open_workflow_export(control, publication) as view:
        assert view.latest()["row"]["checkpoint_version"] == 4
        assert view.summary()["records"] == 4


@pytest.mark.parametrize("state", ["prepared", "published_durability_unknown"])
def test_incomplete_or_unconfirmed_roots_never_authorize_cold_read(
    control: OperatorControlService, state: str
) -> None:
    if state == "prepared":
        with (
            patch.object(WorkflowArtifactStore, "put", side_effect=OSError),
            pytest.raises(OSError),
        ):
            run(control)
    else:
        with patch.object(WorkflowArtifactStore, "flush", return_value=False):
            run(control)
    item = control.workflow_exports()["items"][0]
    assert item["status"] == state
    with pytest.raises(IntegrityError, match="accepted root required"):
        control.verify_workflow_export(item["publication_id"])
    assert control.workflow_exports()["items"][0]["status"] == state
    assert control.kernel.admission.active_operations == 0


@pytest.mark.parametrize("fence", ["active", "quarantine", "migration", "unowned", "drain"])
def test_reader_obeys_runtime_fences(
    control: OperatorControlService, publication: str, fence: str
) -> None:
    if fence == "active":
        control.kernel.resume("Synthetic resumed")
    elif fence == "quarantine":
        control.kernel.admission.quarantine()
    elif fence == "migration":
        with control.kernel.admission.migration_control_scope():
            control.kernel.admission.fence_for_migration(timeout=0)
    elif fence == "unowned":
        control.kernel.process_lock.release()
    else:
        with (
            control.kernel.admission.operation("other", allow_quarantine=True),
            pytest.raises(OperationInvalidated),
        ):
            control.verify_workflow_export(publication)
        return
    with pytest.raises((OperationInvalidated, RuntimeOwnershipError)):
        control.verify_workflow_export(publication)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "key", "schema", "receipt"])
def test_reader_refuses_missing_or_changed_independent_dependencies(
    control: OperatorControlService, publication: str, damage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if damage in {"missing", "corrupt"}:
        ring = ArchiveKeyring.from_env()
        with control.database.read_transaction() as connection:
            record = WorkflowExportCatalog(control.database, control.subject_id).get(
                connection, publication
            )
        assert record is not None
        files = WorkflowArtifactStore(
            control.database,
            control.subject_id,
            publication,
            ring,
            record["encryption_key_id"],
            record["encryption_key_fingerprint"],
            create=False,
        )
        path = files.root / (publication + "_segment-000000.enc")
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"damaged ciphertext")
    elif damage == "key":
        monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", "")
    elif damage == "schema":
        monkeypatch.setattr("noyra.core.workflow_archive_control._SCHEMA_DDL_FINGERPRINTS", {})
    else:
        with control.database.transaction() as c:
            trigger = c.execute(
                "SELECT sql FROM sqlite_master WHERE name='prevent_event_immutable_update'"
            ).fetchone()[0]
            c.execute("DROP TRIGGER prevent_event_immutable_update")
            c.execute(
                "UPDATE events SET source='changed' WHERE event_type='workflow_export_accepted'"
            )
            c.execute(trigger)
    with (
        patch.object(WorkflowArtifactStore, "put", side_effect=AssertionError("repair write")),
        pytest.raises((IntegrityError, ArchiveKeyUnavailableError, OSError, ValueError)),
    ):
        control.verify_workflow_export(publication)
    assert control.kernel.admission.active_operations == 0


def test_privacy_intent_during_file_read_blocks_before_exposure(
    control: OperatorControlService, publication: str
) -> None:
    from noyra.interaction.conversations import ConversationStore

    conversation = ConversationStore(control.database).create(control.subject_id, "Private")
    original = WorkflowArtifactStore.get
    changed = False

    def reading(self: WorkflowArtifactStore, entry: dict[str, Any], *, maximum: int) -> bytes:
        nonlocal changed
        result = original(self, entry, maximum=maximum)
        if not changed:
            with control.database.transaction() as c:
                c.execute(
                    "UPDATE private_conversations SET status='deleting' WHERE conversation_id=?",
                    (conversation["conversation_id"],),
                )
            changed = True
        return result

    with patch.object(WorkflowArtifactStore, "get", reading), pytest.raises(OperationInvalidated):
        control.verify_workflow_export(publication)
    assert control.kernel.admission.active_operations == 0


def test_lease_revocation_and_new_privacy_intent_invalidate_open_view(
    control: OperatorControlService, publication: str
) -> None:
    from noyra.interaction.conversations import ConversationStore

    conversation = ConversationStore(control.database).create(control.subject_id, "Private")
    with open_workflow_export(control, publication) as view:
        with control.database.transaction() as c:
            c.execute(
                "UPDATE private_conversations SET status='deleting' WHERE conversation_id=?",
                (conversation["conversation_id"],),
            )
        with pytest.raises(OperationInvalidated):
            view.latest()
    with control.database.transaction() as c:
        c.execute(
            "UPDATE private_conversations SET status='active' WHERE conversation_id=?",
            (conversation["conversation_id"],),
        )
    with open_workflow_export(control, publication) as view:
        control.kernel.admission.invalidate(closed=True)
        with pytest.raises(OperationInvalidated):
            view.summary()


def test_deadline_and_unknown_financial_evidence_are_preserved(
    control: OperatorControlService, publication: str
) -> None:
    with (
        patch("noyra.core.workflow_archive_control.MAX_SECONDS", 0.0),
        pytest.raises(TimeoutError),
    ):
        control.verify_workflow_export(publication)
    assert control.kernel.admission.active_operations == 0
    with control.database.read_transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM workflow_export_publications").fetchone()[0]
            == 1
        )


def test_invalid_or_foreign_identity_cannot_select_arbitrary_files(
    control: OperatorControlService, publication: str
) -> None:
    for invalid in ["../manifest.enc", "wexport_" + "a" * 64, "", publication + "/anything"]:
        with pytest.raises((ValueError, IntegrityError)):
            control.verify_workflow_export(invalid)
    assert control.kernel.admission.active_operations == 0
    assert Path(control.database.path).is_file()


def test_encrypted_archived_acceptance_and_retired_key_remain_readable(
    control: OperatorControlService, publication: str
) -> None:
    import hashlib
    from datetime import datetime, timedelta

    from noyra.core.archive import ArchiveKeyMaterial
    from noyra.core.event_archive import EventPayloadArchive

    class Future(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Future:
            instant = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(instant.timestamp(), tz)

    archive = EventPayloadArchive(
        control.database,
        control.database.path.parent / "subject/cold",
        subject_id=control.subject_id,
    )
    with patch("noyra.core.event_archive.datetime", Future):
        assert archive.archive_cold(control.subject_id, older_than_days=90) > 0
    old = ArchiveKeyring.from_env().active
    key = bytes(reversed(range(32)))
    active = ArchiveKeyMaterial("rotated", key, hashlib.sha256(key).hexdigest(), "active")
    retained = ArchiveKeyring(
        generation=2,
        active_key_id="rotated",
        legacy_key_id=old.key_id,
        keys=(ArchiveKeyMaterial(old.key_id, old.key, old.fingerprint, "retired"), active),
    )
    retained.record_revision(control.database, control.subject_id)
    with patch(
        "noyra.core.workflow_archive_control.ArchiveKeyring.from_env", return_value=retained
    ):
        assert control.verify_workflow_export(publication)["records"] == 4


def test_unknown_calls_and_reservations_survive_read_verification(
    control: OperatorControlService, publication: str
) -> None:
    from noyra.model import BudgetLimits, ModelLedger

    control.kernel.resume("Synthetic accounting fixture")
    ledger = ModelLedger(control.database)
    call, _ = ledger.prepare_call(
        control.subject_id, "fixture", "fixture", "cold-unknown", "f" * 64, "cold-call"
    )
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(10, 1000, 1000, 1000),
        reserved_input_tokens=11,
        reserved_output_tokens=22,
        reserved_cost_microusd=33,
    )
    ledger.start_attempt(attempt.attempt_id)
    ledger.recover_interrupted(control.subject_id)
    control.kernel.pause("Synthetic verification")
    with control.database.read_transaction() as connection:
        before = {
            table: [dict(row) for row in connection.execute("SELECT * FROM " + table)]
            for table in (
                "model_calls",
                "model_attempts",
                "workflow_checkpoints",
                "events",
                "workflow_export_publications",
                "audit_records",
                "archive_keyring_revisions",
            )
        }
    assert control.verify_workflow_export(publication)["records"] == 4
    with control.database.read_transaction() as connection:
        after = {
            table: [dict(row) for row in connection.execute("SELECT * FROM " + table)]
            for table in before
        }
    assert before == after
    assert before["model_calls"][0]["status"] == "unknown"
    assert before["model_attempts"][0]["reserved_cost_microusd"] == 33
