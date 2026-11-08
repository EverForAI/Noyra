from __future__ import annotations

import copy
import hashlib
import sqlite3
from datetime import datetime, timedelta
from typing import Any, cast
from unittest.mock import patch

import pytest

from noyra.core.admission import OperationInvalidated
from noyra.core.archive import ArchiveKeyMaterial, ArchiveKeyring
from noyra.core.errors import ArchiveKeyUnavailableError, IntegrityError
from noyra.core.types import canonical_json, strict_json_loads
from noyra.core.workflow_chain_control import open_workflow_chain
from noyra.core.workflow_export_files import WorkflowArtifactStore
from noyra.core.workflow_increment_bundle import _reconstruct_increment_chain, accepted_chain
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
from test_d2b_workflow_export_control import control as control
from test_i2_workflow_increment_control import context as context
from test_i3_workflow_chain_control import chain as chain


def test_second_sqlite_pass_detects_changed_reconstructed_data_before_view_exposure(
    chain: Any,
) -> None:
    controls, _base, _first, tip = chain
    connect = sqlite3.connect

    class Damaged(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if sql == "INSERT INTO reconstructed_records VALUES(?,?)" and parameters[0] == 8:
                row = strict_json_loads(parameters[1])
                row["row"]["reason"] = "changed after file verification"
                parameters = (8, canonical_json(row))
            return super().execute(sql, parameters)

    def substituted(path: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if path == ":memory:":
            kwargs["factory"] = Damaged
        return cast(sqlite3.Connection, connect(path, *args, **kwargs))

    with (
        patch("noyra.core.workflow_increment_bundle.sqlite3.connect", substituted),
        pytest.raises(IntegrityError),
        open_workflow_chain(controls, tip),
    ):
        pytest.fail("unverified SQLite view escaped")
    assert controls.kernel.admission.active_operations == 0


def test_every_ancestor_aggregate_is_verified_even_when_final_tip_proof_matches(chain: Any) -> None:
    controls, _base, _first, tip = chain
    catalog = WorkflowIncrementCatalog(controls.database, controls.subject_id)
    with controls.database.read_transaction() as c:
        nodes = accepted_chain(c, catalog, tip, lambda: None)
    # Direct internal proof contract: an altered ancestor source must be caught
    # even when the final complete source and actual record bytes are unchanged.
    altered = copy.deepcopy(nodes)
    altered[0][1]["source"]["history_hash"] = "0" * 64
    with (
        pytest.raises(IntegrityError),
        _reconstruct_increment_chain(
            controls.database, altered, ArchiveKeyring.from_env(), lambda: None
        ),
    ):
        pytest.fail("ancestor proof was replaced by only the final tip proof")


@pytest.mark.parametrize("budget", ["segments", "memory", "deadline"])
def test_cumulative_budgets_never_return_partial_evidence(chain: Any, budget: str) -> None:
    controls, _base, _first, tip = chain
    target, value = {
        "segments": ("noyra.core.workflow_increment_bundle.MAX_CHAIN_SEGMENTS", 2),
        "memory": ("noyra.core.workflow_increment_bundle.MAX_RECONSTRUCTION_BYTES", 1),
        "deadline": ("noyra.core.workflow_chain_control.MAX_SECONDS", 0.0),
    }[budget]
    with (
        patch(target, value),
        pytest.raises((IntegrityError, TimeoutError)),
        open_workflow_chain(controls, tip),
    ):
        pytest.fail("partial evidence escaped after work budget exhausted")
    assert controls.kernel.admission.active_operations == 0


def test_withdrawal_during_file_read_prevents_any_evidence_view(chain: Any) -> None:
    from noyra.interaction.conversations import ConversationStore

    controls, _base, _first, tip = chain
    window = ConversationStore(controls.database).create(
        controls.subject_id, "Synthetic withdrawal"
    )
    original = WorkflowArtifactStore.get
    changed = False

    def withdraw(self: WorkflowArtifactStore, entry: Any, *, maximum: int) -> bytes:
        nonlocal changed
        result = original(self, entry, maximum=maximum)
        if not changed:
            with controls.database.transaction() as c:
                c.execute(
                    "UPDATE private_conversations SET status='deleting' WHERE conversation_id=?",
                    (window["conversation_id"],),
                )
            changed = True
        return result

    with (
        patch.object(WorkflowArtifactStore, "get", withdraw),
        pytest.raises(OperationInvalidated, match="privacy"),
        open_workflow_chain(controls, tip),
    ):
        pytest.fail("withdrawn view escaped")
    assert changed and controls.kernel.admission.active_operations == 0


def test_archived_roots_and_retired_link_keys_remain_readable_after_rotation(chain: Any) -> None:
    from noyra.core.event_archive import EventPayloadArchive

    controls, _base, _first, tip = chain

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
    new = ArchiveKeyMaterial("chain-key", raw, hashlib.sha256(raw).hexdigest(), "active")
    ring = ArchiveKeyring(
        generation=2,
        active_key_id=new.key_id,
        legacy_key_id=old.key_id,
        keys=(ArchiveKeyMaterial(old.key_id, old.key, old.fingerprint, "retired"), new),
    )
    ring.record_revision(controls.database, controls.subject_id)
    with (
        patch("noyra.core.workflow_chain_control.ArchiveKeyring.from_env", return_value=ring),
        open_workflow_chain(controls, tip) as view,
    ):
        assert view.summary()["records"] == 8
        missing = ArchiveKeyring(
            generation=3, active_key_id=new.key_id, legacy_key_id=None, keys=(new,)
        )
        with (
            patch(
                "noyra.core.workflow_chain_control.ArchiveKeyring.from_env", return_value=missing
            ),
            pytest.raises(ArchiveKeyUnavailableError),
        ):
            view.page()


def test_source_and_unknown_financial_state_are_unchanged_by_chain_verification(chain: Any) -> None:
    from noyra.model import BudgetLimits, ModelLedger

    controls, _base, _first, tip = chain
    controls.kernel.resume("Synthetic accounting fixture")
    ledger = ModelLedger(controls.database)
    call, _ = ledger.prepare_call(
        controls.subject_id, "fixture", "fixture", "i3-unknown", "f" * 64, "i3-unknown-key"
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
    controls.kernel.pause("Synthetic verification")
    tables = (
        "model_calls",
        "model_attempts",
        "workflow_checkpoints",
        "workflow_export_publications",
        "workflow_increment_publications",
        "workflow_increment_requests",
        "events",
        "audit_records",
        "archive_keyring_revisions",
        "runtime_state",
    )
    with controls.database.read_transaction() as c:
        before = {
            table: [dict(row) for row in c.execute("SELECT * FROM " + table)] for table in tables
        }
    assert controls.verify_workflow_chain(tip)["records"] == 8
    with controls.database.read_transaction() as c:
        after = {
            table: [dict(row) for row in c.execute("SELECT * FROM " + table)] for table in tables
        }
    assert before == after
    assert after["model_calls"][0]["status"] == "unknown"
    assert after["model_attempts"][0]["reserved_cost_microusd"] == 33


def test_page_byte_bound_and_metadata_checks_are_per_page_not_per_record(chain: Any) -> None:
    from noyra.core import workflow_chain_control

    controls, _base, _first, tip = chain
    with open_workflow_chain(controls, tip) as view:
        size = len(
            view._connection.execute(
                "SELECT record_json FROM reconstructed_records WHERE version=1"
            )
            .fetchone()[0]
            .encode()
        )
        with patch("noyra.core.workflow_reconstruction.MAX_PAGE_BYTES", size):
            limited = view.page()
            assert len(limited["records"]) == 1 and limited["next_after_version"] == 1
            assert limited["truncated"]
        with (
            patch("noyra.core.workflow_reconstruction.MAX_PAGE_BYTES", 1),
            pytest.raises(IntegrityError),
        ):
            view.page()
        original = workflow_chain_control._roots
        with patch.object(workflow_chain_control, "_roots", wraps=original) as checks:
            assert len(view.page()["records"]) == 8
            assert checks.call_count == 2


def test_distinct_parent_and_child_key_dependencies_are_checked_on_open(chain: Any) -> None:
    from noyra.core.workflow_increment_files import WorkflowIncrementStore
    from test_i1_workflow_increment_dependencies import rotated
    from test_i2_workflow_increment_control import advance, publish

    controls, base, _first, tip = chain
    old = ArchiveKeyring.from_env()
    ring = rotated(old)
    ring.record_revision(controls.database, controls.subject_id)
    advance(controls, 1)
    with patch("noyra.core.workflow_chain_control.ArchiveKeyring.from_env", return_value=ring):
        with patch.object(WorkflowIncrementStore, "flush", return_value=True):
            newest = publish((controls, base), parent=tip, version=9, key="rotated-new-tip-key")
        with open_workflow_chain(controls, newest["publication_id"]) as view:
            assert view.summary()["records"] == 9
            assert view.latest()["row"]["checkpoint_version"] == 9
        missing_parent = ArchiveKeyring(
            generation=3, active_key_id=ring.active.key_id, legacy_key_id=None, keys=(ring.active,)
        )
        with (
            patch(
                "noyra.core.workflow_chain_control.ArchiveKeyring.from_env",
                return_value=missing_parent,
            ),
            pytest.raises(ArchiveKeyUnavailableError),
        ):
            controls.verify_workflow_chain(newest["publication_id"])
