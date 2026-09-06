from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from noyra.core import (
    Database,
    EventStore,
    ExportControl,
    ExportJobManager,
    IdentityStore,
    TrainingStore,
)
from noyra.core.errors import TrainingPolicyConflictError
from noyra.core.training_export import (
    TrainingConsentChangedError,
    TrainingDatasetExporter,
)
from noyra.core.types import content_hash


def _database(root: Path, subject_id: str) -> Database:
    database = Database(root / "noyra.sqlite3")
    IdentityStore(database).ensure(subject_id, content_hash({"subject_id": subject_id}))
    TrainingStore(database).ensure_policy(subject_id)
    return database


def test_concurrent_policy_updates_merge_and_preserve_ordered_consent_history(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-p105-policy"
    database = _database(tmp_path, subject_id)
    initial = TrainingStore(database).policy(subject_id)
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}
    failures: list[BaseException] = []
    entered: set[int] = set()
    entered_lock = threading.Lock()
    original_transaction = database.transaction
    changes = {
        "conversations": {"include_conversations": True},
        "workspace": {"include_workspace": True},
    }

    @contextmanager
    def gated_transaction() -> Iterator[Any]:
        thread_id = threading.get_ident()
        wait = False
        with entered_lock:
            if (
                threading.current_thread().name.startswith("p105-policy-")
                and thread_id not in entered
            ):
                entered.add(thread_id)
                wait = True
        if wait:
            barrier.wait(timeout=5)
        with original_transaction() as connection:
            yield connection

    database.transaction = gated_transaction  # type: ignore[method-assign]

    def update(name: str) -> None:
        try:
            TrainingStore(database).update_policy(
                subject_id,
                actor=f"test-{name}",
                reason=f"enable {name}",
                **changes[name],
            )
            outcomes[name] = "committed"
        except TrainingPolicyConflictError:
            outcomes[name] = "conflict"
        except BaseException as error:  # pragma: no cover - surfaced below
            failures.append(error)

    workers = [
        threading.Thread(target=update, args=(name,), name=f"p105-policy-{name}")
        for name in changes
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert not failures
    assert sorted(outcomes.values()) == ["committed", "committed"]
    final = TrainingStore(database).policy(subject_id)
    assert final.policy_version == initial.policy_version + 2
    assert final.include_conversations is True
    assert final.include_workspace is True
    with pytest.raises(TrainingPolicyConflictError):
        TrainingStore(database).update_policy(
            subject_id,
            actor="stale-client",
            reason="stale update must fail",
            expected_version=initial.policy_version,
            include_model_io=True,
        )
    with database.connection() as connection:
        audit_rows = connection.execute(
            "SELECT actor, payload_json FROM audit_records "
            "WHERE subject_id = ? AND action = 'training_policy_updated'",
            (subject_id,),
        ).fetchall()
    audits = sorted(
        ({"actor": row["actor"], **json.loads(row["payload_json"])} for row in audit_rows),
        key=lambda item: item["policy_version"],
    )
    assert [(item["previous_policy_version"], item["policy_version"]) for item in audits] == [
        (initial.policy_version, initial.policy_version + 1),
        (initial.policy_version + 1, initial.policy_version + 2),
    ]
    expected_fields = set(TrainingStore._POLICY_FIELDS) | {"policy_version", "updated_at"}
    for item in audits:
        assert set(item["before"]) == expected_fields
        assert set(item["after"]) == expected_fields
        assert item["before"]["policy_version"] == item["previous_policy_version"]
        assert item["after"]["policy_version"] == item["policy_version"]
        assert item["effective_at"] == item["after"]["updated_at"]


def test_revocation_before_publication_destroys_staged_artifact(tmp_path: Path) -> None:
    subject_id = "Noyra-p105-revocation"
    database = _database(tmp_path, subject_id)
    EventStore(database).append(
        subject_id,
        "p105-training-event",
        "test",
        {"content": "must not publish after revocation"},
        privacy_level="public",
    )
    exporter = TrainingDatasetExporter(database)
    original_publish = exporter._publish_export
    staged_ready = threading.Event()
    release = threading.Event()
    staged_paths: list[Path] = []

    def gated_publish(*args: Any, **kwargs: Any) -> None:
        staged_paths.append(args[-2])
        staged_ready.set()
        if not release.wait(5):
            raise RuntimeError("test publication gate was not released")
        original_publish(*args, **kwargs)

    exporter._publish_export = gated_publish  # type: ignore[method-assign]
    target = tmp_path / "exports" / "training.zip"
    failures: list[BaseException] = []

    def run_export() -> None:
        try:
            exporter.export_to_path(subject_id, actor="test", target=target)
        except BaseException as error:  # pragma: no cover - asserted below
            failures.append(error)

    worker = threading.Thread(target=run_export)
    worker.start()
    assert staged_ready.wait(5)
    assert staged_paths[0].is_file()
    assert not target.exists()

    current = TrainingStore(database).policy(subject_id)
    TrainingStore(database).update_policy(
        subject_id,
        actor="test-revoker",
        reason="withdraw export consent",
        expected_version=current.policy_version,
        export_enabled=False,
    )
    release.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], TrainingConsentChangedError)
    assert not target.exists()
    assert not staged_paths[0].exists()
    assert not list(target.parent.glob(".*.pending"))
    with database.connection() as connection:
        export_count = connection.execute(
            "SELECT COUNT(*) FROM training_exports WHERE subject_id = ?", (subject_id,)
        ).fetchone()[0]
        success_count = connection.execute(
            "SELECT COUNT(*) FROM audit_records "
            "WHERE subject_id = ? AND action = 'training_exported'",
            (subject_id,),
        ).fetchone()[0]
        aborted = connection.execute(
            "SELECT payload_json FROM audit_records "
            "WHERE subject_id = ? AND action = 'training_export_aborted'",
            (subject_id,),
        ).fetchone()
    assert export_count == 0
    assert success_count == 0
    assert aborted is not None
    payload = json.loads(aborted["payload_json"])
    assert payload["lease_policy_version"] == current.policy_version
    assert payload["current_policy_version"] == current.policy_version + 1
    assert payload["staged_disposition"] == "destroyed"


def test_staging_metadata_failure_removes_private_pending_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject_id = "Noyra-p105-stage-failure"
    database = _database(tmp_path, subject_id)
    EventStore(database).append(
        subject_id,
        "p105-stage-failure-event",
        "test",
        {"content": "private staged bytes"},
        privacy_level="public",
    )
    target = tmp_path / "exports" / "training.zip"
    original_metadata = TrainingDatasetExporter._file_metadata

    def fail_metadata(
        path: Path, *, checkpoint: Callable[[], None] | None = None
    ) -> dict[str, int | str]:
        if path.name.endswith(".pending"):
            raise OSError("injected staged metadata failure")
        return original_metadata(path, checkpoint=checkpoint)

    monkeypatch.setattr(TrainingDatasetExporter, "_file_metadata", staticmethod(fail_metadata))
    with pytest.raises(OSError, match="injected staged metadata failure"):
        TrainingDatasetExporter(database).export_to_path(
            subject_id,
            actor="test",
            target=target,
        )

    assert not target.exists()
    assert not list(target.parent.glob(".*.pending"))
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM training_exports WHERE subject_id = ?", (subject_id,)
            ).fetchone()[0]
            == 0
        )


def test_consent_audit_history_is_append_only(tmp_path: Path) -> None:
    subject_id = "Noyra-p105-audit"
    database = _database(tmp_path, subject_id)
    TrainingStore(database).update_policy(
        subject_id,
        actor="audit-test",
        expected_version=1,
        include_conversations=True,
    )
    with database.connection() as connection:
        audit_id = connection.execute(
            "SELECT audit_id FROM audit_records WHERE action = 'training_policy_updated'"
        ).fetchone()["audit_id"]
    with (
        pytest.raises(sqlite3.IntegrityError, match="audit records are append-only"),
        database.transaction() as connection,
    ):
        connection.execute(
            "UPDATE audit_records SET actor = 'tampered' WHERE audit_id = ?",
            (audit_id,),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="audit records cannot be deleted"),
        database.transaction() as connection,
    ):
        connection.execute("DELETE FROM audit_records WHERE audit_id = ?", (audit_id,))


def test_revoked_export_job_never_becomes_downloadable(tmp_path: Path) -> None:
    subject_id = "Noyra-p105-job"
    database = _database(tmp_path, subject_id)
    EventStore(database).append(
        subject_id,
        "p105-job-event",
        "test",
        {"content": "job publication is consent gated"},
        privacy_level="public",
    )
    exporter = TrainingDatasetExporter(database)
    original_publish = exporter._publish_export
    staged_ready = threading.Event()
    release = threading.Event()

    def gated_publish(*args: Any, **kwargs: Any) -> None:
        staged_ready.set()
        if not release.wait(5):
            raise RuntimeError("test job publication gate was not released")
        original_publish(*args, **kwargs)

    exporter._publish_export = gated_publish  # type: ignore[method-assign]

    def run_export(
        subject: str, kind: str, target: Path, control: ExportControl
    ) -> tuple[str, str]:
        assert kind == "training"
        artifact = exporter.export_to_path(
            subject,
            actor="test-job",
            target=target,
            control=control,
        )
        return artifact.filename, artifact.sha256

    manager = ExportJobManager(database, tmp_path / "exports" / "jobs", run_export)
    manager.start_after_ownership(subject_id)
    try:
        job = manager.create(subject_id, "training")
        assert staged_ready.wait(5)
        current = TrainingStore(database).policy(subject_id)
        TrainingStore(database).update_policy(
            subject_id,
            actor="test-job-revoker",
            reason="withdraw consent before job publication",
            expected_version=current.policy_version,
            export_enabled=False,
        )
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = manager.get(job.job_id, subject_id=subject_id)
            if job.status in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert job.status == "failed"
        assert job.error_code == "TrainingConsentChangedError"
        with pytest.raises(RuntimeError, match="not ready"):
            manager.download_path(job.job_id, subject_id=subject_id)
        assert not list((tmp_path / "exports" / "jobs").glob("*.zip"))
    finally:
        release.set()
        manager.close()
