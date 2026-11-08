"""Bounded schema-metadata observations, separate from cleanup authority."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from .lifecycle_inventory import DOMAIN_CONTRACTS, REPRESENTATION_CONTRACTS, TABLE_DOMAINS
from .types import canonical_json, content_hash, strict_json_loads

MAX_OBJECTS = 2000
MAX_PAGE = 100
MAX_BASELINE_BYTES = 524_288
FORMAT = "noyra-lifecycle-catalog/v1"


class LifecycleCatalogUnavailable(RuntimeError):
    pass


def _cursor(value: str, revision: str, schema: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("invalid lifecycle cursor")
    try:
        payload = strict_json_loads(base64.b64decode(value, altchars=b"-_", validate=True))
    except (ValueError, TypeError, UnicodeError, binascii.Error) as error:
        raise ValueError("invalid lifecycle cursor") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"format", "catalog_revision", "schema_inventory", "after"}
        or payload["format"] != FORMAT
        or payload["catalog_revision"] != revision
        or payload["schema_inventory"] != schema
        or not isinstance(payload["after"], str)
        or not 1 <= len(payload["after"]) <= 256
    ):
        raise ValueError("lifecycle cursor changed or is invalid")
    return str(payload["after"])


def lifecycle_catalog(
    connection: Any, *, limit: int = 50, cursor: str | None = None
) -> dict[str, Any]:
    # Lazy import avoids a cycle with the manager facade. The real deletion
    # registry remains authoritative and is only observed here.
    from .retention import (
        RETENTION_REGISTRY,
        RETENTION_REGISTRY_VERSION,
    )

    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError("invalid lifecycle page limit")
    rows = connection.execute(
        "SELECT substr(name,1,257) AS name,length(name) AS name_length FROM sqlite_master "
        "WHERE type='table' AND substr(name,1,7)!='sqlite_' ORDER BY name LIMIT ?",
        (MAX_OBJECTS + 1,),
    ).fetchall()
    if len(rows) > MAX_OBJECTS or any(not 1 <= row["name_length"] <= 256 for row in rows):
        raise LifecycleCatalogUnavailable("schema metadata exceeds lifecycle observation bound")
    names = [str(row["name"]) for row in rows]
    actual = set(names)
    declared = {item.table: item for item in RETENTION_REGISTRY}
    revision = content_hash(
        {
            "format": FORMAT,
            "preservation": dict(TABLE_DOMAINS),
            "domain_contracts": dict(DOMAIN_CONTRACTS),
            "representation_contracts": dict(REPRESENTATION_CONTRACTS),
            "retention_registry": RETENTION_REGISTRY_VERSION,
        }
    )
    baseline_row = connection.execute(
        "SELECT substr(value,1,?) AS value,length(CAST(value AS BLOB)) AS bytes "
        "FROM schema_meta WHERE key='retention_contract_inventory'",
        (MAX_BASELINE_BYTES + 1,),
    ).fetchone()
    baseline_status = "missing" if baseline_row is None else "invalid"
    baseline: set[str] | None = None
    if baseline_row is not None and baseline_row["bytes"] <= MAX_BASELINE_BYTES:
        try:
            value = strict_json_loads(baseline_row["value"])
        except (TypeError, ValueError):
            value = None
        if (
            isinstance(value, list)
            and len(value) <= MAX_OBJECTS
            and all(isinstance(name, str) and 1 <= len(name) <= 256 for name in value)
            and len(set(value)) == len(value)
        ):
            baseline = set(value)
            baseline_status = "ok"
    schema = content_hash(
        {
            "objects": names,
            "schema_cookie": connection.execute("PRAGMA schema_version").fetchone()[0],
            "baseline_status": baseline_status,
            "baseline": None if baseline is None else sorted(baseline),
        }
    )
    after = None if cursor is None else _cursor(cursor, revision, schema)
    if after is not None and after not in actual:
        raise ValueError("lifecycle cursor object is missing")
    selected = [name for name in names if after is None or name > after][: limit + 1]
    unreviewed = actual - TABLE_DOMAINS.keys() - declared.keys()
    items = []
    for name in selected[:limit]:
        rule = declared.get(name)
        state = (
            "implemented_retention_rule"
            if rule is not None
            else "documented_domain_contract"
            if name in DOMAIN_CONTRACTS
            else "preservation_baseline"
            if name in TABLE_DOMAINS
            else "unreviewed_preserve"
        )
        items.append(
            {
                "table": name,
                "domain": TABLE_DOMAINS.get(name),
                "policy_state": state,
                "default_action": "preserve",
                "existing_retention_action": None if rule is None else rule.retention_action,
                "existing_domain_contract": DOMAIN_CONTRACTS.get(name),
                "representation_contract": REPRESENTATION_CONTRACTS.get(name),
                "new_cleanup_authorized": False,
                "reference_review": "domain_contract_required_for_any_new_cleanup",
            }
        )
    structural_unknown = sorted(actual if baseline is None else actual - baseline)
    structural_missing = [] if baseline is None else sorted(baseline - actual)
    next_cursor = None
    if len(selected) > limit:
        next_cursor = base64.urlsafe_b64encode(
            canonical_json(
                {
                    "format": FORMAT,
                    "catalog_revision": revision,
                    "schema_inventory": schema,
                    "after": selected[limit - 1],
                }
            ).encode()
        ).decode()
    return {
        "format": FORMAT,
        "scope": "database_schema_metadata_not_subject_contents",
        "catalog_revision": revision,
        "schema_inventory": schema,
        "policy_coverage": {
            "status": "review_required" if unreviewed else "known_preservation_inventory",
            "advisory_only": True,
            "objects": len(names),
            "implemented_retention_objects": len(actual & declared.keys()),
            "documented_domain_objects": len((actual & DOMAIN_CONTRACTS.keys()) - declared.keys()),
            "preservation_only_objects": len(
                (actual & TABLE_DOMAINS.keys()) - declared.keys() - DOMAIN_CONTRACTS.keys()
            ),
            "unreviewed_objects": len(unreviewed),
            "absent_compatibility_rules": sorted(declared.keys() - actual)[:MAX_PAGE],
            "absent_compatibility_rule_count": len(declared.keys() - actual),
            "absent_compatibility_rules_truncated": len(declared.keys() - actual) > MAX_PAGE,
            "new_cleanup_authorized": False,
        },
        "structural_drift": {
            "scope": "table_name_inventory_not_complete_ddl_verification",
            "status": "baseline_unavailable"
            if baseline is None
            else ("mismatch" if structural_unknown or structural_missing else "matches_baseline"),
            "baseline_status": baseline_status,
            "unclassified": list(structural_unknown[:MAX_PAGE]),
            "unclassified_count": len(structural_unknown),
            "missing": list(structural_missing[:MAX_PAGE]),
            "missing_count": len(structural_missing),
            "truncated": len(structural_unknown) > MAX_PAGE or len(structural_missing) > MAX_PAGE,
        },
        "items": items,
        "next_cursor": next_cursor,
        "page": {
            "returned": len(items),
            "record_limit": limit,
            "has_more": next_cursor is not None,
        },
    }
