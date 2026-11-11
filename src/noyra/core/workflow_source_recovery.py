"""Compare reconstructed history for an existing publication without changing it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schema.contracts import _SCHEMA_DDL_FINGERPRINTS, _SCHEMA_STRUCTURE_FINGERPRINTS
from .types import canonical_json
from .workflow_bundle import source_identity, validate_source


@dataclass(frozen=True)
class RecoverySchemaPair:
    """One directed, explicitly reviewed trigger-only schema transition."""

    source_version: int
    source_ddl: str
    target_version: int
    target_ddl: str
    structure: str


# Exact reviewed trigger-only migration117. No adjacent/transitive inference.
_REVIEWED_SCHEMA_PAIRS: tuple[RecoverySchemaPair, ...] = (
    RecoverySchemaPair(
        116,
        "e78f5e9d12d781e71b504dae847c793e680be7788af14d8af5b769f4cd4cd847",
        117,
        "6ddaea3121a169783d2151c7dfb6907ed41ad3c98ed7bb978fc45503b122f41d",
        "2c848bc479a0b2ae97c2ef91d271e01fdf8363110892d7f681412dae64e488d0",
    ),
)


def matches_publication_source(
    actual: dict[str, Any], frozen: dict[str, Any], *, existing_request: bool
) -> bool:
    """Require the same business evidence, allowing only a reviewed schema pair.

    The controller obtains ``existing_request`` from its validated immutable
    request lookup, before preparing any new intent. Neither user input nor an
    artifact can opt in. ``actual`` comes from a strict live schema check and a
    complete history reconstruction. The caller still checks the frozen event
    prefix and actual archive objects. This function neither edits nor returns
    a normalized source: writers keep the original frozen evidence.
    """
    validate_source(actual)
    validate_source(frozen)
    if source_identity(actual) == source_identity(frozen):
        return True
    if existing_request is not True:
        return False
    for pair in _REVIEWED_SCHEMA_PAIRS:
        if not (
            pair.target_version > pair.source_version
            and pair.source_version == frozen["schema_version"]
            and pair.source_ddl == frozen["schema_ddl_hash"]
            and pair.target_version == actual["schema_version"]
            and pair.target_ddl == actual["schema_ddl_hash"]
            and _SCHEMA_DDL_FINGERPRINTS.get(pair.source_version) == pair.source_ddl
            and _SCHEMA_DDL_FINGERPRINTS.get(pair.target_version) == pair.target_ddl
            and _SCHEMA_STRUCTURE_FINGERPRINTS.get(pair.source_version) == pair.structure
            and _SCHEMA_STRUCTURE_FINGERPRINTS.get(pair.target_version) == pair.structure
        ):
            continue
        # validate_source fixes the exact field set. Event heads may advance
        # without business changes; controllers separately verify the old anchor.
        excluded = {"schema_version", "schema_ddl_hash", "event_head"}
        return canonical_json({k: v for k, v in actual.items() if k not in excluded}) == (
            canonical_json({k: v for k, v in frozen.items() if k not in excluded})
        )
    return False
