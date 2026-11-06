from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.archive import ArchiveKeyring
from noyra.core.operator_controls import OperatorControlService
from noyra.core.workflow_export_files import WorkflowArtifactStore
from test_m42_p3_06_operator_controls import _kernel


@pytest.fixture
def control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    kernel = _kernel(tmp_path, "d2b-control")
    workflow = DurableWorkflowStore(kernel.database)
    for n in range(4):
        workflow.checkpoint(
            kernel.subject_id,
            "export-workflow",
            "fixture",
            "running",
            {"n": n},
            reason="Synthetic private state",
        )
    controls = OperatorControlService(kernel)
    kernel.pause("Synthetic maintenance")
    try:
        yield controls
    finally:
        kernel.close()


def run(
    control: OperatorControlService, key: str = "synthetic-request", version: int = 4
) -> dict[str, Any]:
    return control.export_workflow(
        "export-workflow", version, key, actor="operator", reason="Synthetic reviewed export"
    )


def test_published_files_have_explicit_durability_and_source_is_untouched(
    control: OperatorControlService,
) -> None:
    import os

    from noyra.core.types import content_hash

    with control.database.read_transaction() as c:
        before = content_hash(
            [dict(row) for row in c.execute("SELECT * FROM workflow_checkpoints")]
        )
    result = run(control)
    assert result["readback_verified"]
    assert result["status"] == ("published_durability_unknown" if os.name == "nt" else "accepted")
    assert result["durability_confirmed"] == (os.name != "nt")
    assert result["records"] == 4
    assert (
        control.kernel.lifecycle.current().state == "paused"
        and not control.kernel.admission.accepting
    )
    assert control.kernel.admission.active_operations == 0
    with control.database.read_transaction() as c:
        assert (
            content_hash([dict(row) for row in c.execute("SELECT * FROM workflow_checkpoints")])
            == before
        )
        assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
    assert control.workflow_exports()["items"][0]["publication_id"] == result["publication_id"]


def test_acceptance_is_atomic_idempotent_and_independent_of_ciphertext(
    control: OperatorControlService,
) -> None:
    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        first = run(control)
        assert first["status"] == "accepted"
        with patch.object(
            WorkflowArtifactStore, "put", side_effect=AssertionError("accepted files overwritten")
        ):
            again = run(control)
            another = run(control, key="another-request-key")
    assert again["idempotent"] and another["idempotent"]
    assert first["publication_id"] == again["publication_id"] == another["publication_id"]
    with control.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM workflow_export_publications").fetchone()[0] == 1
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='workflow_export_accepted'"
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM audit_records WHERE action='workflow_export_accepted'"
            ).fetchone()[0]
            == 1
        )
        ring = ArchiveKeyring.from_env()
        ring.validate_revision(control.database, control.subject_id, connection=c)
    with pytest.raises(ValueError, match="already used"):
        run(control, key="another-request-key", version=5)


@pytest.mark.parametrize("fence", ["active", "quarantine", "migration", "unowned"])
def test_control_rejects_invalid_runtime_before_preparing_exports(
    control: OperatorControlService, fence: str
) -> None:
    from noyra.core.admission import OperationInvalidated
    from noyra.core.errors import RuntimeOwnershipError

    with control.database.read_transaction() as c:
        key_history_before = c.execute("SELECT COUNT(*) FROM archive_keyring_revisions").fetchone()[
            0
        ]
    if fence == "active":
        control.kernel.resume("Synthetic active runtime")
    elif fence == "quarantine":
        control.kernel.admission.quarantine()
    elif fence == "migration":
        with control.kernel.admission.migration_control_scope():
            control.kernel.admission.fence_for_migration(timeout=0)
    else:
        control.kernel.process_lock.release()
    with pytest.raises((OperationInvalidated, RuntimeOwnershipError)):
        run(control)
    with control.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM workflow_export_publications").fetchone()[0] == 0
        assert (
            c.execute("SELECT COUNT(*) FROM archive_keyring_revisions").fetchone()[0]
            == key_history_before
        )
    assert not (control.database.path.parent / "subject").exists()


def test_control_does_not_run_alongside_other_admitted_work(
    control: OperatorControlService,
) -> None:
    from noyra.core.admission import OperationInvalidated

    with (
        control.kernel.admission.operation("synthetic-other-control", allow_quarantine=True),
        pytest.raises(OperationInvalidated),
    ):
        run(control)
    assert control.kernel.admission.active_operations == 0


def test_acceptance_and_private_audit_roll_back_together_then_retry(
    control: OperatorControlService,
) -> None:
    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        with (
            patch.object(
                control, "_audit_connection", side_effect=OSError("synthetic audit fault")
            ),
            pytest.raises(OSError),
        ):
            run(control)
        with control.database.read_transaction() as c:
            assert (
                c.execute("SELECT status FROM workflow_export_publications").fetchone()[0]
                == "prepared"
            )
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type='workflow_export_accepted'"
                ).fetchone()[0]
                == 0
            )
        recovered = run(control)
        assert recovered["status"] == "accepted"
    assert len(control.workflow_exports()["items"]) == 1


def test_interrupted_artifact_write_keeps_prepared_request_recoverable(
    control: OperatorControlService,
) -> None:
    original = WorkflowArtifactStore.put

    def interrupted(self: WorkflowArtifactStore, name: str, value: bytes) -> dict[str, Any]:
        result = original(self, name, value)
        if name.startswith("segment-"):
            raise OSError("synthetic file interruption")
        return result

    with patch.object(WorkflowArtifactStore, "put", interrupted), pytest.raises(OSError):
        run(control)
    assert control.workflow_exports()["items"][0]["status"] == "prepared"
    assert run(control)["readback_verified"]


def test_publication_unknown_can_promote_only_after_readback_and_durability(
    control: OperatorControlService,
) -> None:
    with patch.object(WorkflowArtifactStore, "flush", return_value=False):
        published = run(control)
    assert (
        published["status"] == "published_durability_unknown"
        and not published["durability_confirmed"]
    )
    with (
        patch.object(
            WorkflowArtifactStore, "put", side_effect=AssertionError("rewrote published objects")
        ),
        patch.object(WorkflowArtifactStore, "flush", return_value=True),
    ):
        accepted = run(control)
    assert (
        accepted["publication_id"] == published["publication_id"]
        and accepted["status"] == "accepted"
    )


def test_changed_source_during_file_io_never_receives_acceptance(
    control: OperatorControlService,
) -> None:
    from noyra.core.errors import IntegrityError
    from noyra.core.types import canonical_json, content_hash

    original = WorkflowArtifactStore.put

    def changed(self: WorkflowArtifactStore, name: str, value: bytes) -> dict[str, Any]:
        result = original(self, name, value)
        if name == "manifest.enc":
            with control.database.transaction() as c:
                state = {"rewritten": "synthetic"}
                c.execute(
                    "UPDATE workflow_checkpoints SET state_json=?,state_hash=? "
                    "WHERE checkpoint_version=1",
                    (canonical_json(state), content_hash(state)),
                )
        return result

    with (
        patch.object(WorkflowArtifactStore, "put", changed),
        patch.object(WorkflowArtifactStore, "flush", return_value=True),
        pytest.raises(ValueError, match="database changed"),
    ):
        run(control)
    assert control.workflow_exports()["items"][0]["status"] == "prepared"
    with pytest.raises(IntegrityError, match="source history changed"):
        run(control)


def test_pause_epoch_invalidated_during_write_cannot_commit_acceptance(
    control: OperatorControlService,
) -> None:
    from noyra.core.admission import OperationInvalidated

    original = WorkflowArtifactStore.put

    def invalidated(self: WorkflowArtifactStore, name: str, value: bytes) -> dict[str, Any]:
        result = original(self, name, value)
        control.kernel.admission.invalidate()
        return result

    with (
        patch.object(WorkflowArtifactStore, "put", invalidated),
        pytest.raises(OperationInvalidated),
    ):
        run(control)
    assert control.workflow_exports()["items"][0]["status"] == "prepared"
    assert control.kernel.admission.active_operations == 0
    assert run(control)["readback_verified"]


def test_referenced_key_cannot_be_removed_and_retired_key_can_read_publication(
    control: OperatorControlService,
) -> None:
    import hashlib

    from noyra.core.archive import ArchiveKeyMaterial
    from noyra.core.errors import ArchiveKeyUnavailableError

    published = run(control)
    old = ArchiveKeyring.from_env().active
    new = ArchiveKeyMaterial(
        "new-key",
        bytes(reversed(range(32))),
        hashlib.sha256(bytes(reversed(range(32)))).hexdigest(),
        "active",
    )
    missing = ArchiveKeyring(generation=2, active_key_id="new-key", legacy_key_id=None, keys=(new,))
    with pytest.raises(ArchiveKeyUnavailableError):
        missing.validate_revision(control.database, control.subject_id)
    retained = ArchiveKeyring(
        generation=2,
        active_key_id="new-key",
        legacy_key_id=old.key_id,
        keys=(ArchiveKeyMaterial(old.key_id, old.key, old.fingerprint, "retired"), new),
    )
    retained.record_revision(control.database, control.subject_id)
    with patch("noyra.core.workflow_export_control.ArchiveKeyring.from_env", return_value=retained):
        result = run(control)
    assert result["publication_id"] == published["publication_id"] and result["readback_verified"]


def test_missing_accepted_object_never_triggers_an_automatic_reexport(
    control: OperatorControlService,
) -> None:
    from noyra.core.errors import IntegrityError

    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        result = run(control)
    ring = ArchiveKeyring.from_env()
    store = WorkflowArtifactStore(
        control.database,
        control.subject_id,
        result["publication_id"],
        ring,
        ring.active.key_id,
        ring.active.fingerprint,
        create=False,
    )
    (store.root / (store.publication + "_manifest.enc")).unlink()
    with (
        patch.object(WorkflowArtifactStore, "put", side_effect=AssertionError("resend/overwrite")),
        pytest.raises((IntegrityError, RuntimeError, OSError)),
    ):
        run(control)
    assert control.workflow_exports()["items"][0]["status"] == "accepted"
    assert control.kernel.lifecycle.current().state == "paused"


def test_unknown_attempt_reservations_are_preserved(control: OperatorControlService) -> None:
    from noyra.model import BudgetLimits, ModelLedger

    control.kernel.resume("Synthetic accounting fixture")
    ledger = ModelLedger(control.database)
    call, _ = ledger.prepare_call(
        control.subject_id, "fixture", "fixture", "synthetic-unknown", "f" * 64, "synthetic-call"
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
    control.kernel.pause("Synthetic export")
    with control.database.read_transaction() as c:
        before = {
            table: [dict(row) for row in c.execute("SELECT * FROM " + table)]
            for table in ("model_calls", "model_attempts", "workflow_checkpoints")
        }
    run(control)
    with control.database.read_transaction() as c:
        after = {
            table: [dict(row) for row in c.execute("SELECT * FROM " + table)] for table in before
        }
    assert after == before and before["model_calls"][0]["status"] == "unknown"
    assert before["model_attempts"][0]["reserved_cost_microusd"] == 33


def test_catalog_rejects_missing_or_rehashed_acceptance_evidence(
    control: OperatorControlService,
) -> None:
    import sqlite3
    from contextlib import closing

    from noyra.core.errors import IntegrityError

    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        run(control)
    with closing(sqlite3.connect(control.database.path)) as c:
        c.execute("PRAGMA foreign_keys=OFF")
        for (name,) in c.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='events'"
        ).fetchall():
            c.execute('DROP TRIGGER "' + name + '"')
        c.execute("DELETE FROM events WHERE event_type='workflow_export_accepted'")
        c.commit()
    with pytest.raises(IntegrityError):
        control.workflow_exports()


def test_reopen_prepared_export_recovers_original_request(control: OperatorControlService) -> None:
    from noyra.core import SubjectKernel
    from noyra.core.types import content_hash

    original = WorkflowArtifactStore.put

    def interrupted(self: WorkflowArtifactStore, name: str, value: bytes) -> dict[str, Any]:
        original(self, name, value)
        raise OSError("synthetic process boundary")

    with patch.object(WorkflowArtifactStore, "put", interrupted), pytest.raises(OSError):
        run(control)
    subject, path = control.subject_id, control.database.path
    control.kernel.close()
    with SubjectKernel(
        path, subject, content_hash({"subject_id": subject, "test": "p3-06"})
    ) as reopened:
        assert reopened.boot().state == "paused"
        restarted = OperatorControlService(reopened)
        assert run(restarted)["readback_verified"]
        assert len(restarted.workflow_exports()["items"]) == 1
        assert reopened.lifecycle.current().state == "paused" and not reopened.admission.accepting


def test_budget_timeout_keeps_original_history_and_releases_lease(
    control: OperatorControlService,
) -> None:
    with patch("noyra.core.workflow_export_control.MAX_SECONDS", 0.0), pytest.raises(TimeoutError):
        run(control)
    assert control.kernel.admission.active_operations == 0
    with control.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0] == 4
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='workflow_export_accepted'"
            ).fetchone()[0]
            == 0
        )


def test_export_directory_and_legacy_privacy_review_keep_publication_scope(
    control: OperatorControlService,
) -> None:
    from noyra.interaction.erasure import ConversationErasure, ErasureReviewRequired

    run(control)
    with (
        control.database.read_transaction() as c,
        pytest.raises(ErasureReviewRequired, match="legacy_export"),
    ):
        ConversationErasure._review_exports(c, control.subject_id)
    assert control.workflow_exports()["items"][0]["restore_or_dispatch_authorized"] is False


def test_historical_privacy_intent_blocks_new_export(control: OperatorControlService) -> None:
    from noyra.core.admission import OperationInvalidated
    from noyra.interaction.conversations import ConversationStore

    window = ConversationStore(control.database).create(control.subject_id, "Private window")
    with control.database.transaction() as c:
        c.execute(
            "UPDATE private_conversations SET status='deleting' WHERE conversation_id=?",
            (window["conversation_id"],),
        )
    with pytest.raises(OperationInvalidated, match="privacy"):
        run(control)
    assert not control.workflow_exports()["items"]


def test_acceptance_receipts_survive_encrypted_event_archive_and_reopen(
    control: OperatorControlService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timedelta

    from noyra.core.event_archive import EventPayloadArchive

    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        accepted = run(control)

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
    assert control.workflow_exports()["items"][0]["status"] == "accepted"
    result = run(control)
    assert result["publication_id"] == accepted["publication_id"] and result["idempotent"]


def test_existing_integrity_check_validates_publication_receipts(
    control: OperatorControlService,
) -> None:
    from noyra.core.integrity import IntegrityRegistry

    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        run(control)
    report = IntegrityRegistry().run(
        control.database,
        control.subject_id,
        control.database.path.parent,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=5,
        check_ids=("operations.retention",),
    )
    assert not report.findings
    assert report.checks[0].details["workflow_exports"] == 1


def test_readback_mismatch_never_commits_an_acceptance(control: OperatorControlService) -> None:
    from noyra.core.errors import IntegrityError

    with (
        patch(
            "noyra.core.workflow_export_control.verify_bundle",
            side_effect=IntegrityError("synthetic readback fault"),
        ),
        pytest.raises(IntegrityError),
    ):
        run(control)
    assert control.workflow_exports()["items"][0]["status"] == "prepared"
    with control.database.read_transaction() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='workflow_export_accepted'"
            ).fetchone()[0]
            == 0
        )
    assert run(control)["readback_verified"]


def test_missing_key_or_storage_fault_does_not_mutate_source_or_acceptance(
    control: OperatorControlService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.core.errors import ArchiveKeyUnavailableError

    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", "")
    with pytest.raises(ArchiveKeyUnavailableError):
        run(control)
    assert not control.workflow_exports()["items"]
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    with (
        patch.object(
            WorkflowArtifactStore, "flush", side_effect=OSError("synthetic directory flush failure")
        ),
        pytest.raises(OSError),
    ):
        run(control)
    assert control.workflow_exports()["items"][0]["status"] == "prepared"
    assert run(control)["readback_verified"]


def test_durability_promotion_rechecks_source_changes_between_calls(
    control: OperatorControlService,
) -> None:
    from noyra.core.errors import IntegrityError
    from noyra.core.types import canonical_json, content_hash

    with patch.object(WorkflowArtifactStore, "flush", return_value=False):
        result = run(control)
    with control.database.transaction() as c:
        state = {"source": "changed since publication"}
        c.execute(
            "UPDATE workflow_checkpoints SET state_json=?,state_hash=? WHERE checkpoint_version=1",
            (canonical_json(state), content_hash(state)),
        )
    with (
        patch.object(WorkflowArtifactStore, "flush", return_value=True),
        pytest.raises(IntegrityError),
    ):
        run(control)
    assert control.workflow_exports()["items"][0]["status"] == "published_durability_unknown"
    assert result["source_history_preserved"]


@pytest.mark.parametrize("stage", ["flush", "audit"])
def test_deadline_expiring_before_final_commit_prevents_acceptance(
    control: OperatorControlService, stage: str
) -> None:
    audit = control._audit_connection

    def finish_after_deadline(_store: WorkflowArtifactStore) -> bool:
        # A blocked filesystem call cannot be preempted. Once it returns,
        # the expired deadline must prevent acceptance.
        if stage == "flush":
            clock.return_value = 1000.0
        return True

    def audit_after_deadline(*args: Any, **kwargs: Any) -> None:
        audit(*args, **kwargs)
        if stage == "audit":
            clock.return_value = 1000.0

    with (
        patch("noyra.core.workflow_export_control.time.monotonic", return_value=0.0) as clock,
        patch.object(WorkflowArtifactStore, "flush", finish_after_deadline),
        patch.object(control, "_audit_connection", audit_after_deadline),
        pytest.raises(TimeoutError),
    ):
        run(control)
    assert control.kernel.admission.active_operations == 0
    assert control.workflow_exports()["items"][0]["status"] == "prepared"
    with control.database.read_transaction() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_checkpoints"
        ).fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='workflow_export_accepted'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_records WHERE action='workflow_export_accepted'"
        ).fetchone()[0] == 0
