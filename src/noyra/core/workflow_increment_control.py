"""Owned incremental publication: full source proof, fenced acceptance and audit."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Any

from .admission import OperationInvalidated, OperationLease, bind_lease
from .archive import ArchiveKeyring
from .types import canonical_json, content_hash
from .workflow_bundle import chain_head, check_anchor, inspect_source, require, source_identity
from .workflow_export_control import _guard
from .workflow_export_files import PUBLICATION
from .workflow_increment_bundle import (
    Chain,
    accepted_chain,
    artifact_store,
    verify_increment_chain,
    verify_source_prefix,
    write_increment,
)
from .workflow_increment_catalog import WorkflowIncrementCatalog
from .workflow_increment_contract import INCREMENT, compatible, publication_id
from .workflow_publication_events import PublicationEvents

if TYPE_CHECKING:
    from .operator_controls import OperatorControlService

MAX_SECONDS = 120.0


def export_increment(
    controls: OperatorControlService,
    workflow_id: str,
    parent_id: str,
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
        raise ValueError("invalid increment workflow")
    if not isinstance(parent_id, str) or not (
        PUBLICATION.fullmatch(parent_id) or INCREMENT.fullmatch(parent_id)
    ):
        raise ValueError("invalid increment parent")
    if type(expected_version) is not int or expected_version < 2:
        raise ValueError("invalid increment expected version")
    if (
        not isinstance(request_key, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_key) is None
    ):
        raise ValueError("invalid increment request key")
    with (
        controls.kernel.admission.operation("workflow-increment", allow_quarantine=True) as lease,
        bind_lease(lease),
    ):
        _guard(controls, lease)
        controls._require_resume_health()
        return _export(
            controls,
            lease,
            workflow_id,
            parent_id,
            expected_version,
            content_hash(request_key),
            actor=actor,
            reason=reason,
        )


def _export(
    controls: OperatorControlService,
    lease: OperationLease,
    workflow: str,
    parent_id: str,
    version: int,
    request_hash: str,
    *,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    database = controls.database
    proofs = PublicationEvents(database)
    catalog = WorkflowIncrementCatalog(database, controls.subject_id, events=proofs)
    deadline = time.monotonic() + MAX_SECONDS
    checked_at = 0.0
    final_connection: Any | None = None

    def checkpoint() -> None:
        nonlocal checked_at
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError("workflow increment deadline exceeded")
        gate = controls.kernel.admission
        if gate.epoch != lease.epoch or gate.closed or gate.quarantined or gate.migration_fenced:
            raise OperationInvalidated("workflow increment operation invalidated")
        if now - checked_at >= 0.1:
            if final_connection is not None:
                _guard(controls, lease, final_connection)
            else:
                with database.read_transaction() as guard_connection:
                    _guard(controls, lease, guard_connection)
            checked_at = now

    @contextmanager
    def capture() -> Iterator[Any]:
        with database.read_snapshot(checkpoint=checkpoint) as snapshot:
            snapshot.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                _guard(controls, lease, snapshot)
                database._validate_schema_contract_connection(
                    snapshot, strict=True, persist_marker=False
                )
                yield snapshot
            finally:
                snapshot.set_progress_handler(None, 0)

    def parents(snapshot: Any) -> Chain:
        nodes = accepted_chain(snapshot, catalog, parent_id, checkpoint)
        root = nodes[0][1]["source"]
        identity = snapshot.execute(
            "SELECT genesis_hash FROM subject_identity WHERE subject_id=?", (controls.subject_id,)
        ).fetchone()
        require(
            root["workflow_id"] == workflow
            and identity is not None
            and root["genesis_hash"] == identity["genesis_hash"],
            "increment parent workflow or genesis differs",
        )
        chain_head(snapshot, controls.subject_id, checkpoint)
        return nodes

    ring = ArchiveKeyring.from_env()
    ring.validate_revision(database, controls.subject_id)
    # Look up the frozen intent before inspecting the current workflow version.
    with database.read_transaction() as c:
        _guard(controls, lease, c)
        saved = catalog.requested(c, request_hash, parent_id, workflow, version)
    if saved is None:
        ring.record_revision(database, controls.subject_id)
        with capture() as snapshot:
            original = inspect_source(snapshot, database, controls.subject_id, workflow, checkpoint)
            if original["records"] != version:
                raise ValueError("increment source version changed")
            nodes = parents(snapshot)
            compatible(nodes[-1][1]["source"], original)
            verify_increment_chain(database, nodes, ring, checkpoint)
            verify_source_prefix(snapshot, database, nodes[-1][1], checkpoint)
            # Another request may already name this logical source. Prime its
            # archived immutable receipts before metadata preparation takes a writer.
            catalog.get(snapshot, publication_id(parent_id, original))
        with (
            controls._lock,
            controls.kernel.admission.commit_scope(lease),
            controls.kernel.process_lock.ownership_scope(),
            proofs.no_archive_reads(),
            database.transaction() as c,
        ):
            checkpoint()
            _guard(controls, lease, c)
            saved = catalog.prepare(c, original, parent_id, request_hash, ring)
    require(saved is not None, "increment preparation missing")
    assert saved is not None
    _guard(controls, lease)
    files = artifact_store(database, saved, ring, base=False, create=saved["status"] == "prepared")
    with database.connection() as current:
        current.execute("PRAGMA busy_timeout=2000")
        # Values are connection-local: retain this connection through final CAS.
        data_version = current.execute("PRAGMA data_version").fetchone()[0]
        manifest = saved["manifest"]
        with capture() as snapshot:
            nodes = parents(snapshot)
            frozen = catalog.get(snapshot, saved["publication_id"])
            require(
                frozen is not None and canonical_json(frozen) == canonical_json(saved),
                "increment preparation changed",
            )
            check_anchor(snapshot, controls.subject_id, saved["source"]["event_head"])
            if saved["status"] != "accepted":
                original = inspect_source(
                    snapshot, database, controls.subject_id, workflow, checkpoint
                )
                require(
                    source_identity(original) == saved["source_hash"],
                    "increment frozen source changed",
                )
                verify_source_prefix(snapshot, database, nodes[-1][1], checkpoint)
                if saved["status"] == "prepared":
                    manifest = write_increment(snapshot, database, files, saved, checkpoint)
        candidate = {**saved, "manifest": manifest}
        verification_nodes = [*nodes, ("increment", candidate)]
        verify_increment_chain(database, verification_nodes, ring, checkpoint)
        checkpoint()
        ring = ArchiveKeyring.from_env()
        ring.validate_revision(database, controls.subject_id)
        for _kind, node in verification_nodes:
            ring.resolve(node["encryption_key_id"], node["encryption_key_fingerprint"])
        durable = files.flush()
        _guard(controls, lease)
        controls._require_resume_health()
        checkpoint()
        with (
            controls._lock,
            controls.kernel.admission.commit_scope(lease),
            controls.kernel.process_lock.ownership_scope(),
            proofs.no_archive_reads(),
        ):
            try:
                current.execute("BEGIN IMMEDIATE")
                final_connection = current
                checkpoint()
                # Privacy withdrawal is a terminal control condition, not a
                # generic source-change retry. Recheck it unconditionally on
                # this locked transaction; checkpoints may be throttled.
                _guard(controls, lease, current)
                if current.execute("PRAGMA data_version").fetchone()[0] != data_version:
                    raise ValueError(
                        "increment source database changed during publication; "
                        "retry original intent"
                    )
                ring.validate_revision(database, controls.subject_id, connection=current)
                for _kind, node in verification_nodes:
                    ring.resolve(node["encryption_key_id"], node["encryption_key_fingerprint"])
                frozen = catalog.requested(current, request_hash, parent_id, workflow, version)
                require(
                    frozen is not None and canonical_json(frozen) == canonical_json(saved),
                    "increment request changed before acceptance",
                )
                before = saved["status"]
                result = catalog.finish(current, saved, manifest, durable=durable)
                if before != result["status"]:
                    controls._audit_connection(
                        current,
                        "workflow_increment_" + result["status"],
                        actor,
                        reason,
                        {
                            "publication_id": result["publication_id"],
                            "source_hash": result["source_hash"],
                            "parent_id": parent_id,
                        },
                    )
                checkpoint()
                current.commit()
            except BaseException:
                with suppress(Exception):
                    current.rollback()
                raise
            finally:
                final_connection = None
    return {
        **catalog.public(result),
        "idempotent": saved["status"] == result["status"],
        "readback_verified": True,
        "durability_confirmed": result["status"] == "accepted",
    }
