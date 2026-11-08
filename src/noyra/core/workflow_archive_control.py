"""Read accepted workflow roots under owned, paused and privacy-aware admission."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from .admission import OperationInvalidated, OperationLease, bind_lease
from .archive import ArchiveKeyring
from .schema.contracts import _SCHEMA_DDL_FINGERPRINTS
from .types import content_hash
from .workflow_bundle import chain_head, check_anchor, require
from .workflow_export_catalog import WorkflowExportCatalog
from .workflow_export_control import _guard
from .workflow_export_files import PUBLICATION, WorkflowArtifactStore
from .workflow_reconstruction import _reconstruct, _WorkflowEvidence

if TYPE_CHECKING:
    from .operator_controls import OperatorControlService

MAX_SECONDS = 120.0


def _accepted_root(
    controls: OperatorControlService,
    lease: OperationLease,
    publication: str,
    checkpoint: Callable[[], None],
    *,
    deep: bool,
) -> tuple[dict[str, Any], ArchiveKeyring]:
    checkpoint()
    ring = ArchiveKeyring.from_env()
    with controls.database.read_transaction() as connection:
        connection.execute("PRAGMA query_only=ON")

        # Interrupt long schema/count scans too. SQLite converts a progress
        # callback exception to an interrupted query; the caller fails closed.
        def progress() -> int:
            checkpoint()
            return 0

        connection.set_progress_handler(progress, 1000)
        _guard(controls, lease, connection)
        if deep:
            controls.database._validate_schema_contract_connection(
                connection, strict=True, persist_marker=False
            )
        record = WorkflowExportCatalog(controls.database, controls.subject_id).get(
            connection, publication
        )
        require(record is not None and record["status"] == "accepted", "accepted root required")
        assert record is not None
        source = record["source"]
        identity = connection.execute(
            "SELECT genesis_hash FROM subject_identity WHERE subject_id=?", (controls.subject_id,)
        ).fetchone()
        require(
            identity is not None and identity["genesis_hash"] == source["genesis_hash"],
            "accepted root genesis differs",
        )
        require(
            _SCHEMA_DDL_FINGERPRINTS.get(source["schema_version"]) == source["schema_ddl_hash"],
            "accepted source schema is unsupported",
        )
        ring.validate_revision(controls.database, controls.subject_id, connection=connection)
        ring.resolve(record["encryption_key_id"], record["encryption_key_fingerprint"])
        if deep:
            chain_head(connection, controls.subject_id, checkpoint)
            check_anchor(connection, controls.subject_id, source["event_head"])
        checkpoint()
        return record, ring


@contextmanager
def open_workflow_export(
    controls: OperatorControlService, publication: str
) -> Iterator[_WorkflowEvidence]:
    """A context-bound evidence reader. It never repairs, accepts or activates anything."""
    if not isinstance(publication, str) or PUBLICATION.fullmatch(publication) is None:
        raise ValueError("invalid workflow publication identity")
    with (
        controls.kernel.admission.operation(
            "workflow-archive-read", allow_quarantine=True
        ) as lease,
        bind_lease(lease),
    ):
        deadline = time.monotonic() + MAX_SECONDS
        checked_at = 0.0

        def checkpoint() -> None:
            nonlocal checked_at
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError("workflow archive read deadline exceeded")
            gate = controls.kernel.admission
            if (
                gate.epoch != lease.epoch
                or gate.closed
                or gate.quarantined
                or gate.migration_fenced
            ):
                raise OperationInvalidated("workflow archive read invalidated")
            if now - checked_at >= 0.1:
                _guard(controls, lease)
                checked_at = now

        _guard(controls, lease)
        controls._require_resume_health()
        checkpoint()
        record, ring = _accepted_root(controls, lease, publication, checkpoint, deep=True)
        root_hash = content_hash(record)
        store = WorkflowArtifactStore(
            controls.database,
            controls.subject_id,
            publication,
            ring,
            record["encryption_key_id"],
            record["encryption_key_fingerprint"],
            create=False,
        )
        with _reconstruct(store, record["manifest"], record["source"], checkpoint) as view:
            latest, _ = _accepted_root(controls, lease, publication, checkpoint, deep=True)
            require(
                content_hash(latest) == root_hash, "accepted root changed during reconstruction"
            )
            controls._require_resume_health()
            checkpoint()

            def access() -> None:
                checkpoint()
                current, _ = _accepted_root(controls, lease, publication, checkpoint, deep=False)
                require(content_hash(current) == root_hash, "accepted root changed during reading")

            view._checkpoint = access
            yield view
