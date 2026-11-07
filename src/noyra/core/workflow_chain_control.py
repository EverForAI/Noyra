"""Context-bound accepted chain reading with current privacy and ownership controls."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from .admission import OperationInvalidated, OperationLease, bind_lease
from .archive import ArchiveKeyring
from .types import content_hash
from .workflow_bundle import chain_head, require
from .workflow_export_control import _guard
from .workflow_export_files import PUBLICATION
from .workflow_increment_bundle import Chain, _reconstruct_increment_chain, accepted_chain
from .workflow_increment_catalog import WorkflowIncrementCatalog
from .workflow_increment_contract import INCREMENT
from .workflow_reconstruction import _WorkflowEvidence

if TYPE_CHECKING:
    from .operator_controls import OperatorControlService

MAX_SECONDS = 120.0


class _ChainEvidence(_WorkflowEvidence):
    def __init__(self, verified: _WorkflowEvidence, nodes: Chain, access: Callable[[], None]):
        super().__init__(verified._connection, verified._source, verified._checkpoint)
        self._nodes = nodes
        self._access = access

    def _ready(self) -> None:
        super()._ready()
        self._access()

    def summary(self) -> dict[str, Any]:
        result = super().summary()
        return {
            **result,
            "format": "workflow-chain-evidence/v1",
            "tip_id": self._nodes[-1][1]["publication_id"],
            "base_id": self._nodes[0][1]["publication_id"],
            "chain_depth": len(self._nodes) - 1,
            "chain_nodes": len(self._nodes),
            "ancestor_boundaries_verified": len(self._nodes),
        }


def _roots(
    controls: OperatorControlService,
    lease: OperationLease,
    tip: str,
    checkpoint: Callable[[], None],
    *,
    deep: bool,
) -> tuple[Chain, ArchiveKeyring]:
    checkpoint()
    ring = ArchiveKeyring.from_env()
    with controls.database.read_transaction() as c:
        c.execute("PRAGMA query_only=ON")

        interrupted: Exception | None = None

        def progress() -> int:
            nonlocal interrupted
            try:
                checkpoint()
            except Exception as error:
                interrupted = error
                return 1
            return 0

        c.set_progress_handler(progress, 1000)
        try:
            _guard(controls, lease, c)
            if deep:
                controls.database._validate_schema_contract_connection(
                    c, strict=True, persist_marker=False
                )
            nodes = accepted_chain(
                c, WorkflowIncrementCatalog(controls.database, controls.subject_id), tip, checkpoint
            )
            identity = c.execute(
                "SELECT genesis_hash FROM subject_identity WHERE subject_id=?",
                (controls.subject_id,),
            ).fetchone()
            require(
                identity is not None
                and all(
                    row["source"]["subject_id"] == controls.subject_id
                    and row["source"]["genesis_hash"] == identity["genesis_hash"]
                    for _kind, row in nodes
                ),
                "chain current subject or genesis differs",
            )
            ring.validate_revision(controls.database, controls.subject_id, connection=c)
            for _kind, row in nodes:
                ring.resolve(row["encryption_key_id"], row["encryption_key_fingerprint"])
            if deep:
                chain_head(c, controls.subject_id, checkpoint)
            checkpoint()
            return nodes, ring
        except sqlite3.OperationalError as error:
            if interrupted is not None:
                raise interrupted from error
            raise
        finally:
            c.set_progress_handler(None, 0)


@contextmanager
def open_workflow_chain(
    controls: OperatorControlService,
    tip_id: str,
) -> Iterator[_ChainEvidence]:
    """Read an explicitly selected accepted tip; never mutates or activates a runtime."""
    if not isinstance(tip_id, str) or not (
        PUBLICATION.fullmatch(tip_id) or INCREMENT.fullmatch(tip_id)
    ):
        raise ValueError("invalid workflow chain tip")
    with (
        controls.kernel.admission.operation("workflow-chain-read", allow_quarantine=True) as lease,
        bind_lease(lease),
    ):
        deadline = time.monotonic() + MAX_SECONDS
        checked_at = 0.0

        def checkpoint() -> None:
            nonlocal checked_at
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError("workflow chain read deadline exceeded")
            gate = controls.kernel.admission
            if (
                gate.epoch != lease.epoch
                or gate.closed
                or gate.quarantined
                or gate.migration_fenced
            ):
                raise OperationInvalidated("workflow chain read invalidated")
            if now - checked_at >= 0.1:
                with controls.database.read_transaction() as live:
                    _guard(controls, lease, live)
                checked_at = now

        _guard(controls, lease)
        controls._require_resume_health()
        checkpoint()
        nodes, ring = _roots(controls, lease, tip_id, checkpoint, deep=True)
        frozen = content_hash(nodes)
        with _reconstruct_increment_chain(controls.database, nodes, ring, checkpoint) as verified:
            current, _ = _roots(controls, lease, tip_id, checkpoint, deep=True)
            require(content_hash(current) == frozen, "accepted chain changed during reconstruction")
            controls._require_resume_health()
            checkpoint()

            def access() -> None:
                # Called before and after a page, and for summary/latest. Row
                # iteration keeps the cheap deadline/lease checkpoints instead
                # of re-reading every ancestor for each individual record.
                current, _ = _roots(controls, lease, tip_id, checkpoint, deep=False)
                require(content_hash(current) == frozen, "accepted chain changed during access")
                # Metadata reads use their own coherent snapshot. Recheck live
                # withdrawal/ownership immediately after it, before returning a page.
                with controls.database.read_transaction() as live:
                    _guard(controls, lease, live)
                checkpoint()

            view = _ChainEvidence(verified, nodes, access)
            try:
                yield view
            finally:
                view._closed = True
