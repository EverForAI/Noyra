"""Versioned increment metadata identities; separate from the v1 base format."""

from __future__ import annotations

import re
from typing import Any

from .schema.contracts import _SCHEMA_DDL_FINGERPRINTS
from .types import content_hash
from .workflow_bundle import MAX_MANIFEST_BYTES, require, source_identity, validate_source
from .workflow_export_files import PUBLICATION
from .workflow_manifest import validate_manifest_descriptor

FORMAT = "noyra-workflow-increment/v1"
CATALOG_FORMAT = "workflow-increment-publication/v1"
INCREMENT = re.compile(r"wdelta_[a-f0-9]{64}\Z")
MAX_CHAIN_NODES = 16
PARENT_FIELDS = frozenset(
    {
        "kind",
        "publication_id",
        "binding_hash",
        "source_json_hash",
        "manifest_hash",
        "acceptance_event_id",
        "acceptance_payload_hash",
        "end_version",
        "chain_depth",
    }
)


def digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def known_source(source: Any) -> None:
    validate_source(source)
    require(
        _SCHEMA_DDL_FINGERPRINTS.get(source["schema_version"]) == source["schema_ddl_hash"],
        "increment source schema is unsupported",
    )


def validate_parent(parent: Any) -> None:
    require(
        isinstance(parent, dict) and set(parent) == PARENT_FIELDS,
        "increment parent fields are invalid",
    )
    kind, identifier = parent["kind"], parent["publication_id"]
    require(
        isinstance(identifier, str)
        and (
            (kind == "base" and PUBLICATION.fullmatch(identifier) is not None)
            or (kind == "increment" and INCREMENT.fullmatch(identifier) is not None)
        ),
        "increment parent identity is invalid",
    )
    require(
        all(
            digest(parent[key])
            for key in (
                "binding_hash",
                "source_json_hash",
                "manifest_hash",
                "acceptance_payload_hash",
            )
        ),
        "increment parent digest is invalid",
    )
    require(
        isinstance(parent["acceptance_event_id"], str)
        and 1 <= len(parent["acceptance_event_id"]) <= 256,
        "increment parent acceptance is invalid",
    )
    require(
        type(parent["end_version"]) is int
        and parent["end_version"] >= 1
        and type(parent["chain_depth"]) is int
        and 0 <= parent["chain_depth"] < MAX_CHAIN_NODES - 1
        and (
            (kind == "base" and parent["chain_depth"] == 0)
            or (kind == "increment" and parent["chain_depth"] >= 1)
        ),
        "increment parent bounds are invalid",
    )


def publication_id(parent_id: str, source: dict[str, Any]) -> str:
    return "wdelta_" + content_hash(
        {
            "format": FORMAT,
            "parent_id": parent_id,
            "source_hash": source_identity(source),
        }
    )


def compatible(parent_source: dict[str, Any], source: dict[str, Any]) -> None:
    known_source(parent_source)
    known_source(source)
    require(
        all(
            source[key] == parent_source[key]
            for key in (
                "subject_id",
                "workflow_id",
                "genesis_hash",
                "schema_version",
                "schema_ddl_hash",
            )
        ),
        "increment parent scope or schema differs",
    )
    require(source["records"] > parent_source["records"], "increment must advance versions")
    before, after = parent_source["event_head"], source["event_head"]
    require(
        after["sequence"] >= before["sequence"]
        and (after["sequence"] != before["sequence"] or after["root_hash"] == before["root_hash"]),
        "increment event provenance regressed",
    )


def validate_manifest(manifest: Any) -> None:
    validate_manifest_descriptor(
        manifest, maximum=MAX_MANIFEST_BYTES, reason="increment manifest descriptor is invalid"
    )
