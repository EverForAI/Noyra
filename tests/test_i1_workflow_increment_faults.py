from __future__ import annotations

from typing import Any

import pytest

from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, strict_json_loads
from test_i1_workflow_increment_catalog import SUBJECT, accepted, advance, prepare
from test_i1_workflow_increment_catalog import database as database
from test_i1_workflow_increment_catalog import metadata as metadata
from test_i1_workflow_increment_catalog import template as template


def remove_guards(c: Any, table: str) -> list[str]:
    rows = c.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ).fetchall()
    for row in rows:
        c.execute('DROP TRIGGER "' + row["name"] + '"')
    return [row["sql"] for row in rows]


def restore(c: Any, sql: list[str]) -> None:
    for statement in sql:
        c.execute(statement)


@pytest.mark.parametrize("kind", ["prepare", "accept", "request", "parent_accept"])
def test_rehashed_event_payload_cannot_replace_independent_frozen_bindings(
    metadata: tuple[Any, ...],
    kind: str,
) -> None:
    db, catalog, _ring, base = metadata
    advance(db)
    record = accepted(metadata)
    event = {
        "prepare": "evt_wincrement_prepared_" + record["publication_id"],
        "accept": record["receipt_event_id"],
        "request": catalog._request_event(content_hash("request-one")),
        "parent_accept": base["receipt_event_id"],
    }[kind]
    with db.transaction() as c:
        guards = remove_guards(c, "events")
        row = c.execute("SELECT payload_json FROM events WHERE event_id=?", (event,)).fetchone()
        payload = strict_json_loads(row[0])
        payload["injected"] = True
        c.execute(
            "UPDATE events SET payload_json=?,payload_hash=? WHERE event_id=?",
            (canonical_json(payload), content_hash(payload), event),
        )
        restore(c, guards)
    with db.read_transaction() as c, pytest.raises(IntegrityError):
        if kind == "request":
            catalog.requested(c, content_hash("request-one"), base["publication_id"], "workflow", 4)
        else:
            catalog.get(c, record["publication_id"])


@pytest.mark.parametrize("kind", ["parent_ref", "request_intent", "key_binding", "ancestor_state"])
def test_rehashing_catalog_row_does_not_forge_preparation_or_parent_receipts(
    metadata: tuple[Any, ...],
    kind: str,
) -> None:
    db, catalog, _ring, base = metadata
    advance(db)
    record = accepted(metadata)
    table = (
        "workflow_increment_requests"
        if kind == "request_intent"
        else "workflow_increment_publications"
    )
    with db.transaction() as c:
        guards = remove_guards(c, table)
        row = dict(c.execute("SELECT * FROM " + table).fetchone())
        if kind == "parent_ref":
            parent = strict_json_loads(row["parent_ref_json"])
            parent["manifest_hash"] = "0" * 64
            row["parent_ref_json"] = canonical_json(parent)
        elif kind == "request_intent":
            row["expected_version"] += 1
        elif kind == "key_binding":
            row["encryption_key_fingerprint"] = "0" * 64
        else:
            row["status"] = "published_durability_unknown"
            row["receipt_event_id"] = None
        row["state_hash"] = content_hash({k: v for k, v in row.items() if k != "state_hash"})
        identifier = "request_hash" if kind == "request_intent" else "publication_id"
        c.execute(
            "UPDATE "
            + table
            + " SET "
            + ",".join(k + "=?" for k in row)
            + " WHERE "
            + identifier
            + "=?",
            (*row.values(), row[identifier]),
        )
        restore(c, guards)
    if kind == "ancestor_state":
        # The altered row alone no longer claims acceptance. It still cannot
        # be used as a parent, even if its local mutable state hash is repaired.
        advance(db)
        with pytest.raises(IntegrityError, match="not accepted"):
            prepare(metadata, record["publication_id"], "child")
    else:
        with db.read_transaction() as c, pytest.raises(IntegrityError):
            if kind == "request_intent":
                catalog.requested(
                    c, content_hash("request-one"), base["publication_id"], "workflow", 4
                )
            else:
                catalog.get(c, record["publication_id"])


def test_foreign_subject_cannot_resolve_publications_or_request_intents(
    metadata: tuple[Any, ...],
) -> None:
    from noyra.core import IdentityStore
    from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog

    db, _catalog, _ring, base = metadata
    advance(db)
    record = accepted(metadata)
    IdentityStore(db).ensure("foreign", content_hash("foreign-genesis"))
    foreign = WorkflowIncrementCatalog(db, "foreign")
    with db.read_transaction() as c:
        assert foreign.get(c, record["publication_id"]) is None
        assert (
            foreign.requested(c, content_hash("request-one"), base["publication_id"], "workflow", 4)
            is None
        )
        with pytest.raises(IntegrityError, match="missing"):
            foreign.parent(c, record["publication_id"])


def test_integrity_detects_unlinked_request_and_broken_receipt(metadata: tuple[Any, ...]) -> None:
    from noyra.core.integrity import IntegrityRegistry

    db, catalog, _ring, _base = metadata
    advance(db)
    prepare(metadata)
    with db.transaction() as c:
        guards = remove_guards(c, "workflow_increment_requests")
        c.execute("DELETE FROM workflow_increment_requests")
        restore(c, guards)
    with db.read_transaction() as c, pytest.raises(IntegrityError, match="no immutable request"):
        catalog.verify_requests(c)
    report = IntegrityRegistry().run(
        db,
        SUBJECT,
        db.path.parent,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("operations.retention",),
    )
    assert report.findings


@pytest.mark.parametrize("field,value", [("high_water", "not-an-aware-time"), ("records", True)])
def test_malformed_frozen_source_is_reported_as_integrity_failure(
    metadata: tuple[Any, ...], field: str, value: Any
) -> None:
    db, catalog, _ring, _base = metadata
    advance(db)
    record = prepare(metadata)
    with db.transaction() as c:
        guards = remove_guards(c, "workflow_increment_publications")
        row = dict(c.execute("SELECT * FROM workflow_increment_publications").fetchone())
        changed = strict_json_loads(row["source_json"])
        changed[field] = value
        row["source_json"] = canonical_json(changed)
        row["state_hash"] = content_hash({k: v for k, v in row.items() if k != "state_hash"})
        c.execute(
            "UPDATE workflow_increment_publications SET source_json=?,state_hash=?",
            (row["source_json"], row["state_hash"]),
        )
        restore(c, guards)
    with db.read_transaction() as c, pytest.raises(IntegrityError):
        catalog.get(c, record["publication_id"])
