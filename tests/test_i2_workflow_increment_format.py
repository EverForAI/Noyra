from __future__ import annotations

import copy
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core.archive import ArchiveKeyring
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, strict_json_loads
from noyra.core.workflow_export_catalog import WorkflowExportCatalog
from noyra.core.workflow_increment_bundle import (
    accepted_chain,
    artifact_store,
    verify_increment_chain,
)
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
from noyra.core.workflow_increment_files import WorkflowIncrementStore
from test_d2b_workflow_export_control import control as control
from test_i2_workflow_increment_control import advance, publish
from test_i2_workflow_increment_control import context as context


def prepared_candidate(context: Any, *, segmented: bool = False) -> tuple[Any, ...]:
    controls, base = context
    if segmented:
        advance(controls, 2)
    with (
        patch.object(WorkflowIncrementStore, "flush", return_value=False),
        patch("noyra.core.workflow_increment_bundle.CHUNK_ROWS", 1 if segmented else 256),
    ):
        result = publish(context, version=8 if segmented else 6)
    ring = ArchiveKeyring.from_env()
    with controls.database.read_transaction() as c:
        catalog = WorkflowIncrementCatalog(controls.database, controls.subject_id)
        record = catalog.get(c, result["publication_id"])
        parents = accepted_chain(c, catalog, base, lambda: None)
    assert record is not None
    return controls, ring, record, parents


@pytest.mark.parametrize("damage", ["reorder", "gap", "boolean", "missing_tail"])
def test_format_validator_rejects_self_consistent_but_invalid_segment_layout(
    context: Any,
    damage: str,
) -> None:
    controls, ring, record, nodes = prepared_candidate(context, segmented=True)
    files = artifact_store(controls.database, record, ring, base=False)
    manifest = strict_json_loads(files.get(record["manifest"], maximum=1024 * 1024))
    assert len(manifest["segments"]) == 4
    if damage == "reorder":
        manifest["segments"].reverse()
    elif damage == "missing_tail":
        manifest["segments"].pop()
    elif damage == "gap":
        manifest["segments"][0]["first"] += 1
    else:
        manifest["segments"][0]["records"] = True
    descriptor = files.put("manifest.enc", canonical_json(manifest).encode())
    candidate = {**record, "manifest": descriptor}
    with pytest.raises(IntegrityError):
        verify_increment_chain(
            controls.database, [*nodes, ("increment", candidate)], ring, lambda: None
        )


def test_exact_parent_acceptance_and_key_binding_cannot_be_replaced(context: Any) -> None:
    controls, ring, record, nodes = prepared_candidate(context)
    replaced = copy.deepcopy(nodes)
    replaced[0][1]["encryption_key_fingerprint"] = "0" * 64
    with pytest.raises(IntegrityError, match="exact parent acceptance"):
        verify_increment_chain(
            controls.database, [*replaced, ("increment", record)], ring, lambda: None
        )


def test_ciphertext_is_bound_to_increment_namespace_and_canonical_name(context: Any) -> None:
    controls, ring, record, _nodes = prepared_candidate(context)
    files = artifact_store(controls.database, record, ring, base=False)
    with controls.database.read_transaction() as c:
        base = WorkflowExportCatalog(controls.database, controls.subject_id).get(c, context[1])
    assert base is not None
    parent_files = artifact_store(controls.database, base, ring, base=True)
    parent_path = parent_files.root / (base["publication_id"] + "_manifest.enc")
    child_path = files.root / (record["publication_id"] + "_manifest.enc")
    child_path.write_bytes(parent_path.read_bytes())
    with pytest.raises(IntegrityError):
        files.get(base["manifest"], maximum=1024 * 1024)
    assert parent_files.root.parent.name == "workflow-exports"
    assert files.root.parent.name == "workflow-increments"
