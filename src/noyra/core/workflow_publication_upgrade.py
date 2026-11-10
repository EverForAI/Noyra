"""Read-only row evidence preflight for the schema117 publication guards.

Runs inside the existing migration transaction. Archived payloads are checked by
immutable expected payload hashes here, without file I/O under the writer lock.
Actual archive availability and full chain integrity retain their runtime gates.
"""

from __future__ import annotations

from typing import Any

from .archive import ArchiveKeyring
from .errors import IntegrityError
from .types import canonical_json, content_hash, strict_json_loads
from .workflow_bundle import SCOPE, require, source_identity
from .workflow_export_catalog import FIELDS as BASE_FIELDS
from .workflow_export_catalog import WorkflowExportCatalog
from .workflow_increment_catalog import FIELDS as INCREMENT_FIELDS
from .workflow_increment_catalog import REQUEST_FIELDS, WorkflowIncrementCatalog
from .workflow_increment_contract import (
    CATALOG_FORMAT,
    MAX_CHAIN_NODES,
    compatible,
    digest,
    known_source,
    publication_id,
    validate_manifest,
    validate_parent,
)


def _event(c: Any, identifier: str, subject: str, kind: str, expected: dict[str, Any]) -> None:
    row = c.execute("SELECT * FROM events WHERE event_id=?", (identifier,)).fetchone()
    require(
        row is not None
        and row["subject_id"] == subject
        and row["event_type"] == kind
        and row["source"]
        == ("workflow_increment" if kind.startswith("workflow_increment_") else "workflow_export")
        and row["privacy_level"] == "private"
        and row["payload_hash"] == content_hash(expected),
        "legacy publication receipt binding differs",
    )
    if row["payload_archive_key"] is None:
        require(
            canonical_json(strict_json_loads(row["payload_json"])) == canonical_json(expected),
            "legacy inline publication receipt differs",
        )


def _record(c: Any, row: Any, *, increment: bool) -> dict[str, Any]:
    require(row is not None, "legacy publication is missing")
    raw = {key: row[key] for key in (INCREMENT_FIELDS if increment else BASE_FIELDS)}
    require(content_hash(raw) == row["state_hash"], "legacy publication state hash differs")
    source = strict_json_loads(row["source_json"])
    known_source(source)
    require(
        source["subject_id"] == row["subject_id"]
        and source["workflow_id"] == row["workflow_id"]
        and source_identity(source) == row["source_hash"]
        and type(row["key_generation"]) is int
        and row["key_generation"] >= 1
        and digest(row["encryption_key_fingerprint"]),
        "legacy publication source or key differs",
    )
    ArchiveKeyring._validate_key_id(row["encryption_key_id"])
    manifest = None if row["manifest_json"] is None else strict_json_loads(row["manifest_json"])
    result = {**raw, "source": source, "manifest": manifest}
    if increment:
        parent = strict_json_loads(row["parent_ref_json"])
        validate_parent(parent)
        require(
            (row["parent_base_id"] is None) != (row["parent_increment_id"] is None)
            and parent["publication_id"] == (row["parent_base_id"] or row["parent_increment_id"])
            and (parent["kind"] == "base") == (row["parent_base_id"] is not None)
            and row["publication_id"] == publication_id(parent["publication_id"], source)
            and type(row["first_version"]) is int
            and row["first_version"] == parent["end_version"] + 1
            and type(row["last_version"]) is int
            and row["last_version"] == source["records"]
            and row["last_version"] >= row["first_version"]
            and type(row["chain_depth"]) is int
            and row["chain_depth"] == parent["chain_depth"] + 1
            and row["chain_depth"] < MAX_CHAIN_NODES,
            "legacy increment parent or range differs",
        )
        result["parent"] = parent
        binding = WorkflowIncrementCatalog._binding(result)
        prepared_id = "evt_wincrement_prepared_" + row["publication_id"]
        accepted_id = "evt_wincrement_accepted_" + row["publication_id"]
        kind = "workflow_increment_"
    else:
        require(
            row["publication_id"] == "wexport_" + row["source_hash"], "legacy base identity differs"
        )
        binding = WorkflowExportCatalog._binding(result)
        prepared_id = "evt_wexport_prepared_" + row["source_hash"]
        accepted_id = "evt_wexport_accepted_" + row["source_hash"]
        kind = "workflow_export_"
    _event(c, prepared_id, row["subject_id"], kind + "prepared", binding)
    if row["status"] == "prepared":
        require(
            manifest is None and row["receipt_event_id"] is None,
            "legacy prepared publication has terminal fields",
        )
    else:
        require(
            row["status"] in {"accepted", "published_durability_unknown"},
            "legacy publication status is invalid",
        )
        validate_manifest(manifest)
        if row["status"] == "accepted":
            require(row["receipt_event_id"] == accepted_id, "legacy acceptance identity differs")
            _event(
                c,
                accepted_id,
                row["subject_id"],
                kind + "accepted",
                {**binding, "manifest": manifest, "scope": SCOPE},
            )
        else:
            require(row["receipt_event_id"] is None, "legacy unknown publication has acceptance")
    return result


def validate_publication_upgrade(c: Any) -> None:
    """Stream existing evidence and fail without repairing or resigning any row."""
    try:
        for row in c.execute("SELECT * FROM workflow_export_publications ORDER BY publication_id"):
            _record(c, row, increment=False)
        for row in c.execute(
            "SELECT * FROM workflow_increment_publications ORDER BY publication_id"
        ):
            child = _record(c, row, increment=True)
            reference = child["parent"]
            increment = reference["kind"] == "increment"
            table = (
                "workflow_increment_publications" if increment else "workflow_export_publications"
            )
            parent = _record(
                c,
                c.execute(
                    "SELECT * FROM " + table + " WHERE publication_id=?",
                    (reference["publication_id"],),
                ).fetchone(),
                increment=increment,
            )
            require(parent["status"] == "accepted", "legacy increment parent is not accepted")
            compatible(parent["source"], child["source"])
            binding = (
                WorkflowIncrementCatalog._binding(parent)
                if increment
                else WorkflowExportCatalog._binding(parent)
            )
            expected = {
                "kind": reference["kind"],
                "publication_id": parent["publication_id"],
                "binding_hash": content_hash(binding),
                "source_json_hash": content_hash(parent["source"]),
                "manifest_hash": content_hash(parent["manifest"]),
                "acceptance_event_id": parent["receipt_event_id"],
                "acceptance_payload_hash": content_hash(
                    {**binding, "manifest": parent["manifest"], "scope": SCOPE}
                ),
                "end_version": parent["source"]["records"],
                "chain_depth": parent["chain_depth"] if increment else 0,
            }
            require(
                canonical_json(reference) == canonical_json(expected),
                "legacy exact increment parent differs",
            )
            require(
                c.execute(
                    "SELECT 1 FROM workflow_increment_requests WHERE publication_id=? "
                    "AND subject_id=? LIMIT 1",
                    (child["publication_id"], child["subject_id"]),
                ).fetchone()
                is not None,
                "legacy increment has no request intent",
            )
        for row in c.execute(
            "SELECT * FROM workflow_increment_requests ORDER BY subject_id,request_hash"
        ):
            raw = {key: row[key] for key in REQUEST_FIELDS}
            require(
                content_hash(raw) == row["state_hash"] and digest(row["request_hash"]),
                "legacy increment request hash differs",
            )
            identifier = "evt_wincrement_requested_" + content_hash(
                [row["subject_id"], row["request_hash"]]
            )
            require(
                row["receipt_event_id"] == identifier, "legacy increment request identity differs"
            )
            _event(
                c,
                identifier,
                row["subject_id"],
                "workflow_increment_requested",
                {"format": CATALOG_FORMAT, **raw},
            )
            target = c.execute(
                "SELECT subject_id,workflow_id,last_version,parent_base_id,parent_increment_id "
                "FROM workflow_increment_publications WHERE publication_id=?",
                (row["publication_id"],),
            ).fetchone()
            require(
                target is not None
                and target["subject_id"] == row["subject_id"]
                and target["workflow_id"] == row["workflow_id"]
                and target["last_version"] == row["expected_version"]
                and (target["parent_base_id"] or target["parent_increment_id"]) == row["parent_id"],
                "legacy increment request target differs",
            )
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrityError("workflow publication migration evidence is invalid") from error
