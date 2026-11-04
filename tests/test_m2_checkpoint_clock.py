from __future__ import annotations

from pathlib import Path

import pytest

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core.database import Database
from noyra.core.identity import IdentityStore
from noyra.core.types import content_hash


@pytest.fixture
def store(tmp_path: Path) -> DurableWorkflowStore:
    database = Database(tmp_path / "clock.sqlite3")
    IdentityStore(database).ensure("one", content_hash("clock"))
    return DurableWorkflowStore(database)


def write(store: DurableWorkflowStore, value: int) -> None:
    store.checkpoint("one", "workflow", "fixture", "running", {"n": value}, reason="fixture")


def test_clock_rollback_uses_one_order_without_corrupting_history(
    store: DurableWorkflowStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("noyra.autonomy.workflow.utc_now", lambda: "2026-11-04T00:00:01+00:00")
    write(store, 1)
    monkeypatch.setattr("noyra.autonomy.workflow.utc_now", lambda: "2026-11-04T00:00:00+00:00")
    write(store, 2)
    assert store.verify_integrity("one") == 2
    assert store.latest("workflow").created_at == "2026-11-04T00:00:01+00:00"


@pytest.mark.parametrize("delta", [0, 1, 86401])
def test_same_second_rollback_restart_and_clock_receipts(
    store: DurableWorkflowStore, monkeypatch: pytest.MonkeyPatch, delta: int
) -> None:
    from datetime import UTC, datetime, timedelta

    from noyra.core.checkpoint_clock import receipt
    from noyra.core.events import EventStore

    origin = datetime(2026, 11, 4, tzinfo=UTC)
    monkeypatch.setattr("noyra.autonomy.workflow.utc_now", lambda: origin.isoformat())
    write(store, 1)
    observed = (origin - timedelta(seconds=delta)).isoformat()
    monkeypatch.setattr("noyra.autonomy.workflow.utc_now", lambda: observed)
    write(store, 2)
    fresh = DurableWorkflowStore(Database(store.database.path, initialize=False))
    assert fresh.verify_integrity("one") == 2
    with store.database.read_transaction() as c:
        row = c.execute("SELECT * FROM workflow_checkpoints WHERE checkpoint_version=2").fetchone()
        saved = receipt(c, row, store.database)
        assert (saved is None) == (delta == 0)
        if saved:
            assert saved["observed_at"] == observed and saved["high_water_at"] == origin.isoformat()
    EventStore(store.database).verify_chain("one")


def legacy(store: DurableWorkflowStore) -> list[dict[str, object]]:
    from noyra.core.types import canonical_json

    with store.database.transaction() as c:
        for version, hour in enumerate([12, 10, 11], 1):
            c.execute(
                "INSERT INTO workflow_checkpoints VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "legacy",
                    "one",
                    "fixture",
                    version,
                    "running",
                    canonical_json({"n": version}),
                    content_hash({"n": version}),
                    "legacy",
                    f"2026-11-04T{hour}:00:00+00:00",
                ),
            )
        return [dict(r) for r in c.execute("SELECT * FROM workflow_checkpoints")]


def test_legacy_inversions_require_explicit_bound_evidence_and_preserve_all_original_rows(
    store: DurableWorkflowStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core.errors import IntegrityError

    before = legacy(store)
    with pytest.raises(IntegrityError):
        store.verify_integrity("one")
    catalog = store.clock_history("one", "legacy")
    assert catalog["unexplained_versions"] == [2, 3]
    with store.database.transaction() as c:
        result = store.acknowledge_clock_history(
            c,
            "one",
            "legacy",
            catalog["expected_fingerprint"],
            actor="operator",
            reason="Verified recorded wall clock against independent evidence",
            evidence_sha256=content_hash("independent synthetic backup"),
        )
    assert result["acknowledged_versions"] == [2, 3]
    assert store.verify_integrity("one") == 3
    with store.database.read_transaction() as c:
        assert [dict(r) for r in c.execute("SELECT * FROM workflow_checkpoints")] == before
    with store.database.transaction() as c:
        assert store.acknowledge_clock_history(
            c,
            "one",
            "legacy",
            catalog["expected_fingerprint"],
            actor="operator",
            reason="Duplicate receipt",
            evidence_sha256=content_hash("same"),
        )["idempotent"]
    monkeypatch.setattr("noyra.autonomy.workflow.utc_now", lambda: "2026-11-04T11:30:00+00:00")
    store.checkpoint("one", "legacy", "fixture", "running", {"n": 4}, reason="new")
    assert store.latest("legacy").created_at == "2026-11-04T12:00:00+00:00"
    assert store.verify_integrity("one") == 4


def test_clock_evidence_tampering_does_not_bypass_history_checks(
    store: DurableWorkflowStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core.errors import IntegrityError

    monkeypatch.setattr("noyra.autonomy.workflow.utc_now", lambda: "2026-11-04T12:00:00+00:00")
    write(store, 1)
    monkeypatch.setattr("noyra.autonomy.workflow.utc_now", lambda: "2026-11-04T10:00:00+00:00")
    write(store, 2)
    with store.database.transaction() as c:
        c.execute("DROP TRIGGER prevent_event_immutable_update")
        c.execute(
            "UPDATE events SET payload_hash=? WHERE event_type='workflow_clock_evidence'",
            ("0" * 64,),
        )
    with pytest.raises(IntegrityError):
        store.verify_integrity("one")


@pytest.mark.parametrize("damage", ["gap", "status", "state_hash", "owner"])
def test_legacy_clock_acknowledgement_cannot_waive_non_time_corruption(
    store: DurableWorkflowStore, damage: str
) -> None:
    from noyra.core.errors import IntegrityError

    legacy(store)
    if damage == "owner":
        IdentityStore(store.database).ensure("other", content_hash("other"))
    with store.database.transaction() as c:
        # Synthetic damaged legacy store; normal workflow records are immutable.
        triggers = c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='workflow_checkpoints'"
        ).fetchall()
        for row in triggers:
            c.execute('DROP TRIGGER "' + row[0] + '"')
        if damage == "gap":
            c.execute("DELETE FROM workflow_checkpoints WHERE checkpoint_version=2")
        elif damage == "status":
            c.execute(
                "UPDATE workflow_checkpoints SET status='completed' WHERE checkpoint_version=1"
            )
        elif damage == "state_hash":
            c.execute(
                "UPDATE workflow_checkpoints SET state_hash=? WHERE checkpoint_version=1",
                ("0" * 64,),
            )
        else:
            c.execute(
                "UPDATE workflow_checkpoints SET subject_id='other' WHERE checkpoint_version=2"
            )
    with pytest.raises((IntegrityError, PermissionError)):
        store.clock_history("one", "legacy")


def test_clock_ack_operator_requires_paused_drained_and_works_in_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.core.operator_controls import OperatorControlConflict, OperatorControlService
    from noyra.core.runtime import SubjectKernel

    kernel = SubjectKernel(tmp_path / "clock.sqlite3", "one", content_hash("clock"))
    kernel.boot()
    kernel.orient()
    kernel.activate()
    try:
        store = DurableWorkflowStore(kernel.database)
        before = legacy(store)
        controls = OperatorControlService(kernel)
        catalog = controls.workflow_clock_history("legacy")
        args = {
            "expected_fingerprint": catalog["expected_fingerprint"],
            "evidence_sha256": content_hash("independent backup"),
            "actor": "operator",
            "reason": "Confirmed clock regression from independent evidence",
        }
        with pytest.raises(OperatorControlConflict, match="requires_paused"):
            controls.acknowledge_workflow_clock("legacy", **args)
        lease = kernel.admission.begin("in-flight")
        controls.pause(actor="operator", reason="Clock recovery")
        with pytest.raises(OperatorControlConflict, match="not_drained"):
            controls.acknowledge_workflow_clock("legacy", **args)
        kernel.admission.finish(lease)
        kernel.admission.quarantine()
        original = controls._audit_connection

        def broken(*args: object, **kwargs: object) -> None:
            raise RuntimeError("synthetic audit failure")

        monkeypatch.setattr(controls, "_audit_connection", broken)
        with pytest.raises(RuntimeError, match="audit failure"):
            controls.acknowledge_workflow_clock("legacy", **args)
        assert store.clock_history("one", "legacy")["unexplained_versions"] == [2, 3]
        monkeypatch.setattr(controls, "_audit_connection", original)
        assert controls.acknowledge_workflow_clock("legacy", **args)["acknowledged_versions"] == [
            2,
            3,
        ]
        assert kernel.admission.quarantined and kernel.lifecycle.current().state == "paused"
        assert store.verify_integrity("one") == 3
        with kernel.database.read_transaction() as c:
            assert [dict(r) for r in c.execute("SELECT * FROM workflow_checkpoints")] == before
    finally:
        kernel.close()


def test_clock_receipt_survives_real_encrypted_event_archiving(
    store: DurableWorkflowStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64
    from datetime import datetime, timedelta
    from typing import Any
    from unittest.mock import patch

    from noyra.core.event_archive import EventPayloadArchive
    from noyra.core.events import EventStore

    monkeypatch.setattr("noyra.autonomy.workflow.utc_now", lambda: "2026-08-04T12:00:00+00:00")
    write(store, 1)
    monkeypatch.setattr("noyra.autonomy.workflow.utc_now", lambda: "2026-08-04T11:59:59+00:00")
    for version in range(2, 7):
        write(store, version)
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )

    class Future(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Future:
            value = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(value.timestamp(), tz)

    archive = EventPayloadArchive(
        store.database, store.database.path.parent / "subject/cold", subject_id="one"
    )
    with patch("noyra.core.event_archive.datetime", Future):
        assert archive.archive_cold("one", older_than_days=90) > 0
    loads = []
    original = EventPayloadArchive.load_segment

    def tracked(self: Any, *args: Any, **kwargs: Any) -> Any:
        loads.append(1)
        return original(self, *args, **kwargs)

    with patch.object(EventPayloadArchive, "load_segment", tracked):
        assert (
            DurableWorkflowStore(Database(store.database.path, initialize=False)).verify_integrity(
                "one"
            )
            == 6
        )
    assert len(loads) == 1
    EventStore(store.database).verify_chain("one")


def test_multiple_legacy_workflows_can_be_acknowledged_separately_without_waiving_others(
    store: DurableWorkflowStore,
) -> None:
    from noyra.core.errors import IntegrityError

    legacy(store)
    with store.database.transaction() as c:
        c.execute(
            "INSERT INTO workflow_checkpoints SELECT 'other',subject_id,workflow_type,"
            "checkpoint_version,status,state_json,state_hash,reason,created_at "
            "FROM workflow_checkpoints WHERE workflow_id='legacy'"
        )
    for name in ("legacy", "other"):
        catalog = store.clock_history("one", name)
        with store.database.transaction() as c:
            store.acknowledge_clock_history(
                c,
                "one",
                name,
                catalog["expected_fingerprint"],
                actor="operator",
                reason="Independent clock evidence verified",
                evidence_sha256=content_hash("external backup"),
            )
        if name == "legacy":
            with pytest.raises(IntegrityError):
                store.verify_integrity("one")
    assert store.verify_integrity("one") == 6
