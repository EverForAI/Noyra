"""Compare reconstructed history for an existing publication without changing it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schema.contracts import _SCHEMA_DDL_FINGERPRINTS, _SCHEMA_STRUCTURE_FINGERPRINTS
from .types import canonical_json
from .workflow_bundle import source_identity, validate_source


@dataclass(frozen=True)
class RecoverySchemaPair:
    """One directed, explicitly reviewed schema transition."""

    source_version: int
    source_ddl: str
    target_version: int
    target_ddl: str
    source_structure: str
    target_structure: str


# Exact reviewed schema transitions only. No adjacent/transitive inference.
_REVIEWED_SCHEMA_PAIRS: tuple[RecoverySchemaPair, ...] = (
    RecoverySchemaPair(
        116,
        "e78f5e9d12d781e71b504dae847c793e680be7788af14d8af5b769f4cd4cd847",
        117,
        "6ddaea3121a169783d2151c7dfb6907ed41ad3c98ed7bb978fc45503b122f41d",
        "2c848bc479a0b2ae97c2ef91d271e01fdf8363110892d7f681412dae64e488d0",
        "2c848bc479a0b2ae97c2ef91d271e01fdf8363110892d7f681412dae64e488d0",
    ),
    RecoverySchemaPair(
        117,
        "6ddaea3121a169783d2151c7dfb6907ed41ad3c98ed7bb978fc45503b122f41d",
        118,
        "f7c40fc2ed563822434454c0af0a3e44a4d6c40babb1a6b861830af91180716d",
        "2c848bc479a0b2ae97c2ef91d271e01fdf8363110892d7f681412dae64e488d0",
        "444938463a1ba013c3e7083fcb8ef55419fee5d17c459528ae6e25de6cab3b26",
    ),
    # Existing prepared publications can span both migrations after one
    # deployment. This exact cumulative bridge is reviewed separately; it is
    # not inferred from the two adjacent pairs.
    RecoverySchemaPair(
        116,
        "e78f5e9d12d781e71b504dae847c793e680be7788af14d8af5b769f4cd4cd847",
        118,
        "f7c40fc2ed563822434454c0af0a3e44a4d6c40babb1a6b861830af91180716d",
        "2c848bc479a0b2ae97c2ef91d271e01fdf8363110892d7f681412dae64e488d0",
        "444938463a1ba013c3e7083fcb8ef55419fee5d17c459528ae6e25de6cab3b26",
    ),
    # v120 only changes reflection guards. Existing publications still require
    # the same checkpoint history, receipts and verified event-chain anchor.
    RecoverySchemaPair(
        119,
        "10e3d5548e307f67f6b5c8222637dac5a951e54b4481073094eb220bda22a041",
        120,
        "de0a5b8461949e2c5ea7426ee03a9e31e9cd0b8d19f07e6b6a143d2728f45bac",
        "c2dad7eaea77a67246b7185cf8b07fee74a39607e5a0cac3924a86a72b48b4ef",
        "c2dad7eaea77a67246b7185cf8b07fee74a39607e5a0cac3924a86a72b48b4ef",
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
            and _SCHEMA_STRUCTURE_FINGERPRINTS.get(pair.source_version) == pair.source_structure
            and _SCHEMA_STRUCTURE_FINGERPRINTS.get(pair.target_version) == pair.target_structure
        ):
            continue
        # validate_source fixes the exact field set. Event heads may advance
        # without business changes; controllers separately verify the old anchor.
        excluded = {"schema_version", "schema_ddl_hash", "event_head"}
        return canonical_json({k: v for k, v in actual.items() if k not in excluded}) == (
            canonical_json({k: v for k, v in frozen.items() if k not in excluded})
        )
    return False
