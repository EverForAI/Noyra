from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import pytest

from noyra.core import Database
from noyra.core.admission import OperationInvalidated
from noyra.core.errors import IntegrityError, SchemaContractError
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import content_hash
from noyra.core.workflow_bundle import inspect_source
from noyra.core.workflow_export_catalog import WorkflowExportCatalog
from noyra.core.workflow_export_files import WorkflowArtifactStore
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
from noyra.core.workflow_increment_files import WorkflowIncrementStore
from noyra.core.workflow_source_recovery import RecoverySchemaPair
from noyra.interaction.conversations import ConversationStore
from support.workflow_recovery_schema import control as control
from support.workflow_recovery_schema import measured_pair, publication_upgrade_runtime
from test_d2b_workflow_export_control import run
from test_i2_workflow_increment_control import advance, publish


@dataclass
class RecoverablePublication:
    controls: OperatorControlService
    parent: str | None
    saved: dict[str, Any]
    pair: RecoverySchemaPair

    @property
    def file_class(self) -> type[WorkflowArtifactStore]:
        return WorkflowArtifactStore if self.parent is None else WorkflowIncrementStore

    def call(self, key: str | None = None) -> dict[str, Any]:
        if self.parent is None:
            return run(self.controls, key=key or "synthetic-request")
        return publish((self.controls, self.parent), key=key or "increment-original-request")

    def files(self) -> dict[str, str]:
        directory = self.controls.database.path.parent / "subject"
        if os.name == "nt":
            directory = Path("\\\\?\\" + str(directory))
        return {
            p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob("*.enc")
        }

    def catalog(self) -> WorkflowExportCatalog | WorkflowIncrementCatalog:
        cls = WorkflowExportCatalog if self.parent is None else WorkflowIncrementCatalog
        return cls(self.controls.database, self.controls.subject_id)

    def rows(self) -> dict[str, list[dict[str, Any]]]:
        with self.controls.database.read_transaction() as c:
            return {
                table: [dict(row) for row in c.execute("SELECT * FROM " + table)]
                for table in (
                    "workflow_checkpoints",
                    "workflow_export_publications",
                    "workflow_increment_publications",
                    "events",
                    "event_chain_roots",
                    "audit_records",
                    "model_calls",
                    "model_attempts",
                    "wallet_payment_executions",
                )
            }

    def upgrade(self) -> None:
        # Database's real versioned migration/backup path, scoped to a test-only
        # trigger and contracts. Production still runs schema116 after M2.2.
        Database(self.controls.database.path, strict_schema_contract=True)

    def assert_history_and_fees(self, before: dict[str, list[dict[str, Any]]]) -> None:
        after = self.rows()
        for table in (
            "workflow_checkpoints",
            "model_calls",
            "model_attempts",
            "wallet_payment_executions",
        ):
            assert after[table] == before[table]


@pytest.fixture(params=("base-prepared", "base-unknown", "increment-prepared", "increment-unknown"))
def recoverable(
    control: OperatorControlService, request: pytest.FixtureRequest
) -> Iterator[RecoverablePublication]:
    kind, state = request.param.split("-")
    parent = None
    if kind == "increment":
        with patch.object(WorkflowArtifactStore, "flush", return_value=True):
            parent = str(run(control)["publication_id"])
        advance(control)
    action = RecoverablePublication(control, parent, {}, measured_pair(control.database))
    if state == "prepared":
        with (
            patch.object(
                action.file_class, "flush", side_effect=OSError("synthetic fsync interruption")
            ),
            pytest.raises(OSError),
        ):
            action.call()
    else:
        with patch.object(action.file_class, "flush", return_value=False):
            action.call()
    directory = control.workflow_exports() if parent is None else control.workflow_increments()
    identifier = directory["items"][0]["publication_id"]
    with control.database.read_transaction() as c:
        saved = action.catalog().get(c, identifier)
    assert saved is not None and saved["source"]["schema_version"] == 116
    assert saved["status"] == (
        "prepared" if state == "prepared" else "published_durability_unknown"
    )
    action.saved = saved
    yield action


def test_real_existing_request_recovers_without_normalizing_live_or_frozen_source(
    recoverable: RecoverablePublication,
) -> None:
    before = recoverable.rows()
    stored = recoverable.files()
    original = dict(recoverable.saved)
    with publication_upgrade_runtime(recoverable.pair):
        recoverable.upgrade()
        controls = recoverable.controls
        with controls.database.read_snapshot() as c:
            actual = inspect_source(
                c, controls.database, controls.subject_id, "export-workflow", lambda: None
            )
        assert (
            actual["schema_version"] == 117
            and actual["schema_ddl_hash"] == recoverable.pair.target_ddl
        )
        # Windows protocol fixture deliberately simulates confirmed flush only;
        # POSIX confirms real fsync. Native Windows creation stays unsupported.
        if os.name == "nt":
            with patch.object(recoverable.file_class, "flush", return_value=True):
                result = recoverable.call()
        else:
            result = recoverable.call()
        assert (
            result["status"] == "accepted"
            and result["publication_id"] == original["publication_id"]
        )
        assert result["readback_verified"] and result["durability_confirmed"]
        with controls.database.read_transaction() as c:
            accepted = recoverable.catalog().get(c, result["publication_id"])
        assert accepted is not None and accepted["source"] == original["source"]
        for field in (
            "source_json",
            "source_hash",
            "encryption_key_id",
            "encryption_key_fingerprint",
            "key_generation",
            "created_at",
        ):
            assert accepted[field] == original[field]
        if recoverable.parent is not None:
            assert accepted["parent_ref_json"] == original["parent_ref_json"]
        if original["status"] == "published_durability_unknown":
            assert (
                accepted["manifest_json"] == original["manifest_json"]
                and recoverable.files() == stored
            )
        done = recoverable.rows()
        with patch.object(
            recoverable.file_class, "put", side_effect=AssertionError("published rewrite")
        ):
            assert recoverable.call()["idempotent"]
        assert recoverable.rows() == done
        evidence = (
            controls.verify_workflow_export(result["publication_id"])
            if recoverable.parent is None
            else controls.verify_workflow_chain(result["publication_id"])
        )
        assert evidence["records"] == (4 if recoverable.parent is None else 6)
        recoverable.assert_history_and_fees(before)
        # Compatibility applies only to the existing request, never a new old-schema child.
        if recoverable.parent is not None:
            unchanged = recoverable.rows()
            with pytest.raises(IntegrityError, match="scope or schema"):
                recoverable.call(key="new-cross-schema-request")
            assert recoverable.rows() == unchanged


def test_unapproved_transition_remains_rejected_without_writes(
    recoverable: RecoverablePublication,
) -> None:
    before = recoverable.rows()
    stored = recoverable.files()
    with publication_upgrade_runtime(recoverable.pair, approved=False):
        recoverable.upgrade()
        with pytest.raises(IntegrityError, match=r"source.*changed"):
            recoverable.call()
    assert recoverable.rows() == before and recoverable.files() == stored


def test_real_advanced_history_is_not_hidden_by_schema_compatibility(
    recoverable: RecoverablePublication,
) -> None:
    with publication_upgrade_runtime(recoverable.pair):
        recoverable.upgrade()
        advance(recoverable.controls, count=1)
        before = recoverable.rows()
        stored = recoverable.files()
        with pytest.raises(IntegrityError, match=r"source.*changed"):
            recoverable.call()
        assert recoverable.rows() == before and recoverable.files() == stored


@pytest.mark.parametrize("fence", ("quarantine", "privacy", "cas-write"))
def test_resumed_publication_cannot_bypass_final_runtime_or_transaction_fences(
    recoverable: RecoverablePublication, fence: Literal["quarantine", "privacy", "cas-write"]
) -> None:
    controls = recoverable.controls
    native_flush = recoverable.file_class.flush
    with publication_upgrade_runtime(recoverable.pair):
        recoverable.upgrade()
        conversation = (
            ConversationStore(controls.database).create(controls.subject_id, "Synthetic privacy")
            if fence == "privacy"
            else None
        )
        before = recoverable.rows()

        def interfere(store: WorkflowArtifactStore) -> bool:
            result = native_flush(store)
            if fence == "quarantine":
                controls.kernel.admission.quarantine()
            elif fence == "privacy":
                assert conversation is not None
                with controls.database.transaction() as c:
                    c.execute(
                        "UPDATE private_conversations SET status='deleting' "
                        "WHERE conversation_id=?",
                        (conversation["conversation_id"],),
                    )
            else:
                with controls.database.transaction() as c:
                    controls._audit_connection(
                        c,
                        "synthetic_concurrent_write",
                        "operator",
                        "Synthetic transaction interference",
                        {},
                    )
            return result

        with (
            patch.object(recoverable.file_class, "flush", autospec=True, side_effect=interfere),
            pytest.raises((OperationInvalidated, ValueError)),
        ):
            recoverable.call()
        with controls.database.read_transaction() as c:
            saved = recoverable.catalog().get(c, recoverable.saved["publication_id"])
        assert saved is not None and saved["status"] == recoverable.saved["status"]
        recoverable.assert_history_and_fees(before)


def test_strict_actual_schema_still_rejects_an_unregistered_trigger_change(
    recoverable: RecoverablePublication,
) -> None:
    with publication_upgrade_runtime(recoverable.pair):
        recoverable.upgrade()
        with recoverable.controls.database.transaction() as c:
            c.execute(
                "CREATE TRIGGER synthetic_unreviewed_drift BEFORE DELETE ON workflow_checkpoints "
                "BEGIN SELECT RAISE(ABORT,'synthetic drift'); END"
            )
        before = recoverable.rows()
        with pytest.raises(SchemaContractError):
            recoverable.call()
        assert recoverable.rows() == before


def test_schema_bridge_does_not_hide_rehashed_same_version_business_changes(
    recoverable: RecoverablePublication,
) -> None:
    from noyra.core.types import canonical_json

    with publication_upgrade_runtime(recoverable.pair):
        recoverable.upgrade()
        with recoverable.controls.database.transaction() as c:
            state = {"synthetic": "revised business evidence"}
            c.execute(
                "UPDATE workflow_checkpoints SET state_json=?,state_hash=? "
                "WHERE workflow_id='export-workflow' AND checkpoint_version=1",
                (canonical_json(state), content_hash(state)),
            )
        before = recoverable.rows()
        stored = recoverable.files()
        with pytest.raises(IntegrityError):
            recoverable.call()
        assert recoverable.rows() == before and recoverable.files() == stored


def test_old_parent_and_existing_request_do_not_authorize_new_cross_schema_work(
    recoverable: RecoverablePublication,
) -> None:
    with publication_upgrade_runtime(recoverable.pair):
        recoverable.upgrade()
        if recoverable.parent is None:
            # A genuinely new base keeps the actual target schema and new identity.
            with patch.object(WorkflowArtifactStore, "flush", return_value=True):
                result = recoverable.call(key="new-target-schema-base")
            assert result["publication_id"] != recoverable.saved["publication_id"]
            with recoverable.controls.database.read_transaction() as c:
                new = recoverable.catalog().get(c, result["publication_id"])
            assert new is not None and new["source"]["schema_version"] == 117
        else:
            before = recoverable.rows()
            with pytest.raises(IntegrityError, match="scope or schema"):
                recoverable.call(key="new-unbound-request")
            assert recoverable.rows() == before


def test_archived_intent_receipts_and_retired_keys_still_verify_on_resumption(
    recoverable: RecoverablePublication,
) -> None:
    from datetime import datetime, timedelta

    from noyra.core.archive import ArchiveKeyMaterial, ArchiveKeyring
    from noyra.core.event_archive import EventPayloadArchive

    controls = recoverable.controls

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
    old = ArchiveKeyring.from_env().active
    raw = bytes(reversed(range(32)))
    new = ArchiveKeyMaterial("recovery-next-key", raw, hashlib.sha256(raw).hexdigest(), "active")
    ring = ArchiveKeyring(
        generation=2,
        active_key_id=new.key_id,
        legacy_key_id=old.key_id,
        keys=(ArchiveKeyMaterial(old.key_id, old.key, old.fingerprint, "retired"), new),
    )
    ring.record_revision(controls.database, controls.subject_id)
    with (
        publication_upgrade_runtime(recoverable.pair),
        patch.object(ArchiveKeyring, "from_env", return_value=ring),
    ):
        recoverable.upgrade()
        with patch.object(recoverable.file_class, "flush", return_value=True):
            result = recoverable.call()
        assert (
            result["status"] == "accepted"
            and result["publication_id"] == recoverable.saved["publication_id"]
        )
        evidence = (
            controls.verify_workflow_export(result["publication_id"])
            if recoverable.parent is None
            else controls.verify_workflow_chain(result["publication_id"])
        )
        assert evidence["complete_history_verified"]
        assert controls.kernel.admission.active_operations == 0
