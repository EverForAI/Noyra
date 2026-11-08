from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core.admission import OperationInvalidated
from noyra.core.archive import ArchiveKeyring
from noyra.core.at_rest import AtRestError
from noyra.core.errors import ArchiveKeyUnavailableError, IntegrityError
from noyra.core.events import EventStore
from noyra.core.workflow_export_files import WorkflowArtifactStore
from noyra.core.workflow_increment_bundle import artifact_store
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
from noyra.core.workflow_increment_files import WorkflowIncrementStore
from test_d2b_workflow_export_control import control as control
from test_d2b_workflow_export_control import run as base_export
from test_i2_workflow_increment_control import advance, publish
from test_i2_workflow_increment_control import context as context


def test_source_advance_does_not_retarget_prepared_or_unconfirmed_original_intent(
    context: Any,
) -> None:
    controls, _ = context
    with patch.object(WorkflowIncrementStore, "flush", return_value=False):
        result = publish(context)
    advance(controls, 1)
    with pytest.raises(IntegrityError, match="frozen source changed"):
        publish(context)
    item = controls.workflow_increments()["items"][0]
    assert item["publication_id"] == result["publication_id"]
    assert item["last_version"] == 6 and item["status"] == "published_durability_unknown"


def test_accepted_missing_object_is_not_automatically_reencoded(context: Any) -> None:
    controls, _ = context
    with patch.object(WorkflowIncrementStore, "flush", return_value=True):
        result = publish(context)
    catalog = WorkflowIncrementCatalog(controls.database, controls.subject_id)
    with controls.database.read_transaction() as c:
        record = catalog.get(c, result["publication_id"])
    assert record is not None
    files = artifact_store(controls.database, record, ArchiveKeyring.from_env(), base=False)
    (files.root / (result["publication_id"] + "_segment-000000.enc")).unlink()
    with (
        patch.object(WorkflowIncrementStore, "put", side_effect=AssertionError("accepted rewrite")),
        pytest.raises(AtRestError),
    ):
        publish(context)
    assert controls.workflow_increments()["items"][0]["status"] == "accepted"


def test_bad_parent_object_prevents_any_new_preparation(context: Any) -> None:
    from noyra.core.workflow_export_catalog import WorkflowExportCatalog

    controls, base = context
    with controls.database.read_transaction() as c:
        record = WorkflowExportCatalog(controls.database, controls.subject_id).get(c, base)
    assert record is not None
    files = artifact_store(controls.database, record, ArchiveKeyring.from_env(), base=True)
    (files.root / (base + "_manifest.enc")).write_bytes(b"damaged base")
    with pytest.raises(IntegrityError):
        publish(context)
    assert not controls.workflow_increments()["items"]


@pytest.mark.parametrize("point", ["initial", "during_files", "before_acceptance"])
def test_privacy_withdrawal_blocks_new_acceptance_and_preserves_reviewable_derivatives(
    context: Any,
    point: str,
) -> None:
    from noyra.interaction.conversations import ConversationStore

    controls, _ = context
    conversation = ConversationStore(controls.database).create(
        controls.subject_id, "Synthetic privacy"
    )

    def withdraw() -> None:
        with controls.database.transaction() as c:
            c.execute(
                "UPDATE private_conversations SET status='deleting' WHERE conversation_id=?",
                (conversation["conversation_id"],),
            )

    original_put = WorkflowIncrementStore.put
    original_flush = WorkflowIncrementStore.flush
    if point == "initial":
        withdraw()

    def writing(self: WorkflowIncrementStore, name: str, data: bytes) -> dict[str, Any]:
        result = original_put(self, name, data)
        if point == "during_files":
            withdraw()
        return result

    def flushing(self: WorkflowIncrementStore) -> bool:
        result = original_flush(self)
        if point == "before_acceptance":
            withdraw()
        return result

    with (
        patch.object(WorkflowIncrementStore, "put", writing),
        patch.object(WorkflowIncrementStore, "flush", flushing),
        pytest.raises(OperationInvalidated, match="privacy"),
    ):
        publish(context)
    with controls.database.read_transaction() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='workflow_increment_accepted'"
            ).fetchone()[0]
            == 0
        )
    assert controls.kernel.admission.active_operations == 0


@pytest.mark.parametrize("point", ["deadline", "flush", "audit"])
def test_expired_work_never_commits_acceptance_or_audit(context: Any, point: str) -> None:
    controls, _ = context
    original_audit = controls._audit_connection

    def flushing(_self: WorkflowIncrementStore) -> bool:
        if point == "flush":
            clock.return_value = 1000.0
        return True

    def auditing(*args: Any, **kwargs: Any) -> None:
        original_audit(*args, **kwargs)
        if point == "audit":
            clock.return_value = 1000.0

    with (
        patch("noyra.core.workflow_increment_control.time.monotonic", return_value=0.0) as clock,
        patch.object(WorkflowIncrementStore, "flush", flushing),
        patch.object(controls, "_audit_connection", auditing),
        patch(
            "noyra.core.workflow_increment_control.MAX_SECONDS",
            0.0 if point == "deadline" else 120.0,
        ),
        pytest.raises(TimeoutError),
    ):
        publish(context)
    assert controls.kernel.admission.active_operations == 0
    with controls.database.read_transaction() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='workflow_increment_accepted'"
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM audit_records WHERE action='workflow_increment_accepted'"
            ).fetchone()[0]
            == 0
        )
        assert c.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0] == 6


def test_readback_failure_and_cumulative_budget_never_accept_candidate(context: Any) -> None:
    controls, _ = context
    with (
        patch(
            "noyra.core.workflow_increment_control.verify_increment_chain",
            side_effect=IntegrityError("synthetic readback"),
        ),
        pytest.raises(IntegrityError),
    ):
        publish(context)
    assert not controls.workflow_increments()["items"]
    with (
        patch("noyra.core.workflow_increment_bundle.MAX_CHAIN_SEGMENTS", 1),
        pytest.raises(IntegrityError, match="cumulative"),
    ):
        publish(context)
    assert controls.workflow_increments()["items"][0]["status"] == "prepared"
    assert publish(context)["readback_verified"]


def test_all_file_readback_is_outside_source_writer_transaction(context: Any) -> None:
    controls, _ = context
    original = WorkflowIncrementStore.get
    count = 0

    def reading(self: WorkflowIncrementStore, entry: Any, *, maximum: int) -> bytes:
        nonlocal count
        with closing(controls.database._connect()) as c:
            c.execute("PRAGMA busy_timeout=0")
            c.execute("BEGIN IMMEDIATE")
            c.rollback()
            count += 1
        return original(self, entry, maximum=maximum)

    with patch.object(WorkflowIncrementStore, "get", reading):
        assert publish(context)["readback_verified"]
    assert count >= 2


def test_unknown_paid_work_and_reservations_are_unchanged(context: Any) -> None:
    from noyra.model import BudgetLimits, ModelLedger

    controls, _ = context
    controls.kernel.resume("Synthetic accounting")
    ledger = ModelLedger(controls.database)
    call, _ = ledger.prepare_call(
        controls.subject_id,
        "fixture",
        "fixture",
        "increment-unknown",
        "f" * 64,
        "increment-unknown-key",
    )
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(10, 1000, 1000, 1000),
        reserved_input_tokens=11,
        reserved_output_tokens=22,
        reserved_cost_microusd=33,
    )
    ledger.start_attempt(attempt.attempt_id)
    ledger.recover_interrupted(controls.subject_id)
    controls.kernel.pause("Synthetic publication")
    with controls.database.read_transaction() as c:
        before = {
            table: [dict(row) for row in c.execute("SELECT * FROM " + table)]
            for table in ("model_calls", "model_attempts", "workflow_checkpoints")
        }
    assert publish(context)["readback_verified"]
    with controls.database.read_transaction() as c:
        after = {
            table: [dict(row) for row in c.execute("SELECT * FROM " + table)] for table in before
        }
    assert before == after
    assert after["model_calls"][0]["status"] == "unknown"
    assert after["model_attempts"][0]["reserved_cost_microusd"] == 33


def test_frozen_key_survives_rotation_and_missing_parent_key_fails_before_writes(
    context: Any,
) -> None:
    from test_i1_workflow_increment_dependencies import rotated

    controls, _ = context
    old = ArchiveKeyring.from_env()
    original = WorkflowIncrementStore.put

    def interrupted(self: WorkflowIncrementStore, name: str, data: bytes) -> dict[str, Any]:
        original(self, name, data)
        raise OSError("synthetic key rotation boundary")

    with patch.object(WorkflowIncrementStore, "put", interrupted), pytest.raises(OSError):
        publish(context)
    saved = controls.workflow_increments()["items"][0]
    ring = rotated(old)
    ring.record_revision(controls.database, controls.subject_id)
    with patch("noyra.core.workflow_increment_control.ArchiveKeyring.from_env", return_value=ring):
        result = publish(context)
    assert result["publication_id"] == saved["publication_id"]
    with controls.database.read_transaction() as c:
        record = WorkflowIncrementCatalog(controls.database, controls.subject_id).get(
            c, saved["publication_id"]
        )
        assert record is not None
        assert record["encryption_key_id"] == old.active.key_id
    missing = ArchiveKeyring(
        generation=3, active_key_id=ring.active.key_id, legacy_key_id=None, keys=(ring.active,)
    )
    with (
        patch(
            "noyra.core.workflow_increment_control.ArchiveKeyring.from_env", return_value=missing
        ),
        pytest.raises(ArchiveKeyUnavailableError),
    ):
        publish(context)


def test_archived_parent_and_request_receipts_do_not_read_files_in_writer_scopes(
    context: Any,
) -> None:
    from datetime import datetime, timedelta

    from noyra.core.archive import LocalArchiveProvider
    from noyra.core.event_archive import EventPayloadArchive

    controls, _ = context
    with patch.object(WorkflowIncrementStore, "flush", return_value=True):
        first = publish(context)

    class Future(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Future:
            instant = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(instant.timestamp(), tz)

    with patch("noyra.core.event_archive.datetime", Future):
        assert (
            EventPayloadArchive(
                controls.database,
                controls.database.path.parent / "subject/cold",
                subject_id=controls.subject_id,
            ).archive_cold(controls.subject_id, older_than_days=90)
            > 0
        )
    original = LocalArchiveProvider.get
    probes = 0

    def checked(self: Any, object_key: str, *, max_bytes: int | None = None) -> bytes:
        nonlocal probes
        with closing(controls.database._connect()) as c:
            c.execute("PRAGMA busy_timeout=0")
            c.execute("BEGIN IMMEDIATE")
            c.rollback()
            probes += 1
        return original(self, object_key, max_bytes=max_bytes)

    with patch.object(LocalArchiveProvider, "get", checked):
        assert publish(context)["idempotent"]
        same = publish(context, key="archived-same-source-new-key")
        assert same["publication_id"] == first["publication_id"] and same["idempotent"]
        advance(controls, 1)
        with patch.object(WorkflowIncrementStore, "flush", return_value=True):
            child = publish(
                context, parent=first["publication_id"], version=7, key="archived-parent-child-key"
            )
        assert child["status"] == "accepted"
    assert probes > 0


def test_unprimed_or_changed_archived_receipt_cannot_fall_back_to_file_io(context: Any) -> None:
    from noyra.core.workflow_publication_events import PublicationEvents

    controls, _ = context
    events = PublicationEvents(controls.database)
    synthetic = {
        "payload_archive_key": "missing-segment",
        "subject_id": controls.subject_id,
        "event_id": "synthetic-unprimed",
        "payload_hash": "0" * 64,
    }
    with events.no_archive_reads(), pytest.raises(IntegrityError, match="not primed"):
        events.payload_from_row(synthetic)


@pytest.mark.parametrize("kind", ["base", "increment"])
@pytest.mark.parametrize("change", ["privacy", "unrelated"])
@pytest.mark.parametrize("checkpoint_due", [False, True])
def test_final_acceptance_guard_precedes_version_retry_independently_of_clock(
    context: Any, kind: str, change: str, checkpoint_due: bool
) -> None:
    from noyra.interaction.conversations import ConversationStore

    controls, _ = context
    conversation = ConversationStore(controls.database).create(
        controls.subject_id, "Synthetic final acceptance guard"
    )
    with controls.database.read_transaction() as c:
        before = {
            table: [dict(row) for row in c.execute("SELECT * FROM " + table)]
            for table in ("workflow_checkpoints", "model_calls", "model_attempts")
        }
        accepted_before = c.execute(
            "SELECT COUNT(*) FROM events WHERE event_type IN "
            "('workflow_export_accepted','workflow_increment_accepted')"
        ).fetchone()[0]
    monotonic = 100.0

    def flush(_self: Any) -> bool:
        nonlocal monotonic
        if change == "privacy":
            with controls.database.transaction() as c:
                c.execute(
                    "UPDATE private_conversations SET status='deleting' WHERE conversation_id=?",
                    (conversation["conversation_id"],),
                )
        else:
            # A valid unrelated commit must still trip the unchanged data-version
            # fence; classifying privacy first must never weaken snapshot CAS.
            EventStore(controls.database).append(
                controls.subject_id, "synthetic_publication_race", "test", {"changed": True}
            )
        if checkpoint_due:
            monotonic += 0.2
        return True

    module = (
        "noyra.core.workflow_" + ("increment" if kind == "increment" else "export") + "_control"
    )
    files = WorkflowIncrementStore if kind == "increment" else WorkflowArtifactStore
    expected = OperationInvalidated if change == "privacy" else ValueError
    message = "privacy" if change == "privacy" else "database changed during publication"
    # Freeze the module-local clock so both sides of the 100ms checkpoint
    # throttle are exercised deterministically, without real sleeps or a global
    # time patch that would change lease/runtime clock semantics.
    with (
        patch(module + ".time", SimpleNamespace(monotonic=lambda: monotonic)),
        patch.object(files, "flush", flush),
        pytest.raises(expected, match=message),
    ):
        if kind == "increment":
            publish(context)
        else:
            base_export(controls, key="final-guard-base", version=6)
    with controls.database.read_transaction() as c:
        assert {
            table: [dict(row) for row in c.execute("SELECT * FROM " + table)] for table in before
        } == before
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type IN "
                "('workflow_export_accepted','workflow_increment_accepted')"
            ).fetchone()[0]
            == accepted_before
        )
        table = (
            "workflow_increment_publications"
            if kind == "increment"
            else "workflow_export_publications"
        )
        pending = c.execute("SELECT status FROM " + table + " WHERE status!='accepted'").fetchall()
        assert len(pending) == 1 and pending[0][0] == "prepared"
    assert controls.kernel.admission.active_operations == 0
    assert controls.kernel.lifecycle.current().state == "paused"
