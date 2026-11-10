"""Owned, paused, drained export control; source rows never leave their original representation."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Any

from .admission import OperationInvalidated, OperationLease, bind_lease
from .archive import ArchiveKeyring
from .types import content_hash
from .workflow_bundle import (
    check_anchor,
    inspect_source,
    require,
    verify_bundle,
    write_bundle,
)
from .workflow_export_catalog import WorkflowExportCatalog
from .workflow_export_files import WorkflowArtifactStore
from .workflow_source_recovery import matches_publication_source

if TYPE_CHECKING:
    from .operator_controls import OperatorControlService

MAX_SECONDS = 120.0


def _guard(
    controls: OperatorControlService, lease: OperationLease, connection: Any | None = None
) -> None:
    lease.assert_current()
    controls.kernel.admission.require_owned(controls.kernel.process_lock)
    if controls.kernel.admission.quarantined or controls.kernel.admission.active_operations != 1:
        raise OperationInvalidated("workflow export requires drained non-quarantined control")
    if controls.kernel.lifecycle.current().state != "paused":
        raise OperationInvalidated("workflow export requires manual pause")
    if controls.at_rest is not None and getattr(controls.at_rest, "required", False):
        controls.at_rest.require_ready()
    if connection is not None and controls.kernel.lifecycle.mutation_guard is not None:
        controls.kernel.lifecycle.mutation_guard(connection)
    if connection is not None:
        if connection.execute(
            "SELECT 1 FROM private_conversations WHERE subject_id=? "
            "AND status IN ('deleting','deleted') LIMIT 1",
            (controls.subject_id,),
        ).fetchone():
            raise OperationInvalidated("workflow export requires privacy derivative review")
        if connection.execute(
            "SELECT 1 FROM private_erasure_jobs WHERE subject_id=? LIMIT 1", (controls.subject_id,)
        ).fetchone():
            raise OperationInvalidated("workflow export requires historical privacy review")


def export_workflow(
    controls: OperatorControlService,
    workflow_id: str,
    expected_version: int,
    request_key: str,
    *,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    actor, reason = controls._validate_actor_reason(actor, reason)
    if (
        not isinstance(workflow_id, str)
        or not 1 <= len(workflow_id) <= 256
        or not workflow_id.strip()
    ):
        raise ValueError("invalid workflow id")
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError("invalid expected workflow version")
    if (
        not isinstance(request_key, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_key) is None
    ):
        raise ValueError("invalid workflow export request key")
    # The existing runtime process lock excludes other processes. This
    # tracked operation and the drained guard exclude overlapping controls
    # before any storage/ledger mutation; no extra unowned lock file is made.
    with (
        controls.kernel.admission.operation("workflow-export", allow_quarantine=True) as lease,
        bind_lease(lease),
    ):
        _guard(controls, lease)
        controls._require_resume_health()
        return _export(
            controls,
            lease,
            workflow_id,
            expected_version,
            content_hash(request_key),
            actor=actor,
            reason=reason,
        )


def _export(
    controls: OperatorControlService,
    lease: OperationLease,
    workflow: str,
    version: int,
    request_hash: str,
    *,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    database = controls.database
    catalog = WorkflowExportCatalog(database, controls.subject_id)
    deadline = time.monotonic() + MAX_SECONDS
    checked_at = 0.0

    def checkpoint() -> None:
        nonlocal checked_at
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError("workflow export work deadline exceeded")
        gate = controls.kernel.admission
        if gate.epoch != lease.epoch or gate.closed or gate.quarantined or gate.migration_fenced:
            raise OperationInvalidated("workflow export operation invalidated")
        if now - checked_at >= 0.1:
            _guard(controls, lease)
            checked_at = now

    @contextmanager
    def capture() -> Iterator[Any]:
        with database.read_snapshot(checkpoint=checkpoint) as snapshot:
            snapshot.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                yield snapshot
            finally:
                snapshot.set_progress_handler(None, 0)

    ring = ArchiveKeyring.from_env()
    ring.validate_revision(database, controls.subject_id)
    with database.read_transaction() as connection:
        _guard(controls, lease, connection)
        saved = catalog.requested(connection, request_hash, workflow, version)
    # Only an intent found by the original request key may cross a reviewed
    # schema transition; preparing a new request never grants that authority.
    existing_request = saved is not None
    # Existing published evidence remains readable/idempotent. Refuse new file
    # writes before allocating an intent, key revision or artifact directory.
    if saved is None or saved["status"] == "prepared":
        WorkflowArtifactStore.require_creation_supported()
    if saved is None:
        ring.record_revision(database, controls.subject_id)
        with capture() as snapshot:
            source = inspect_source(snapshot, database, controls.subject_id, workflow, checkpoint)
            if source["records"] != version:
                raise ValueError("workflow source version changed")
        with (
            controls._lock,
            controls.kernel.admission.commit_scope(lease),
            controls.kernel.process_lock.ownership_scope(),
            database.transaction() as connection,
        ):
            _guard(controls, lease, connection)
            saved = catalog.prepare(connection, source, request_hash, ring)
    require(saved is not None, "publication preparation is missing")
    _guard(controls, lease)
    store = WorkflowArtifactStore(
        database,
        controls.subject_id,
        saved["publication_id"],
        ring,
        saved["encryption_key_id"],
        saved["encryption_key_fingerprint"],
        create=saved["status"] == "prepared",
    )
    # Same persistent connection before capture and final BEGIN IMMEDIATE:
    # PRAGMA data_version values are connection-local, never compared across
    # connections. Any intervening DB writer forces a clean retry.
    with database.connection() as current:
        current.execute("PRAGMA busy_timeout=2000")
        data_version = current.execute("PRAGMA data_version").fetchone()[0]
        manifest = saved["manifest"]
        if saved["status"] != "accepted":
            with capture() as snapshot:
                _guard(controls, lease, snapshot)
                source = inspect_source(
                    snapshot, database, controls.subject_id, workflow, checkpoint
                )
                require(
                    matches_publication_source(
                        source, saved["source"], existing_request=existing_request
                    ),
                    "prepared source history changed",
                )
                check_anchor(snapshot, controls.subject_id, saved["source"]["event_head"])
                if saved["status"] == "prepared":
                    manifest = write_bundle(snapshot, database, store, saved["source"], checkpoint)
        verify_bundle(store, manifest, saved["source"], checkpoint)
        checkpoint()
        # Reload metadata before acceptance: rotation may retain the old key,
        # but removal/rollback/reuse of a referenced key is never accepted.
        ring = ArchiveKeyring.from_env()
        ring.validate_revision(database, controls.subject_id)
        ring.resolve(saved["encryption_key_id"], saved["encryption_key_fingerprint"])
        durable = store.flush()
        _guard(controls, lease)
        controls._require_resume_health()
        checkpoint()
        with (
            controls._lock,
            controls.kernel.admission.commit_scope(lease),
            controls.kernel.process_lock.ownership_scope(),
        ):
            try:
                current.execute("BEGIN IMMEDIATE")
                checkpoint()
                # Privacy withdrawal is a terminal control condition, not a
                # generic source-change retry. Recheck it unconditionally on
                # this locked transaction; checkpoints may be throttled.
                _guard(controls, lease, current)
                if current.execute("PRAGMA data_version").fetchone()[0] != data_version:
                    raise ValueError(
                        "workflow export database changed during publication; "
                        "retry unchanged request"
                    )
                ring.validate_revision(database, controls.subject_id, connection=current)
                before = saved["status"]
                result = catalog.finish(current, saved, manifest, durable=durable)
                if result["status"] != before:
                    controls._audit_connection(
                        current,
                        "workflow_export_" + result["status"],
                        actor,
                        reason,
                        {
                            "publication_id": result["publication_id"],
                            "source_hash": result["source_hash"],
                        },
                    )
                checkpoint()
                current.commit()
            except BaseException:
                with suppress(Exception):
                    current.rollback()
                raise
    return {
        **catalog.public(result),
        "idempotent": result["status"] == saved["status"],
        "readback_verified": True,
        "durability_confirmed": result["status"] == "accepted",
    }
