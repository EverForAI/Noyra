from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from noyra.core import workflow_source_recovery as recovery
from noyra.core.errors import IntegrityError
from noyra.core.schema.contracts import _SCHEMA_DDL_FINGERPRINTS, _SCHEMA_STRUCTURE_FINGERPRINTS
from noyra.core.types import canonical_json
from noyra.core.workflow_bundle import FORMAT, SCOPE


@pytest.fixture
def sources() -> tuple[dict[str, Any], dict[str, Any], recovery.RecoverySchemaPair]:
    frozen: dict[str, Any] = {
        "format": FORMAT,
        "scope": SCOPE,
        "subject_id": "synthetic-subject",
        "workflow_id": "synthetic-workflow",
        "genesis_hash": "b" * 64,
        "schema_version": 116,
        "schema_ddl_hash": _SCHEMA_DDL_FINGERPRINTS[116],
        "records": 4,
        "state_bytes": 8,
        "history_hash": "c" * 64,
        "receipt_count": 0,
        "receipts_hash": "d" * 64,
        "last_row_hash": "e" * 64,
        "high_water": "2026-11-10T00:00:00+00:00",
        "event_head": {"sequence": 0, "root_hash": None},
    }
    pair = recovery.RecoverySchemaPair(
        116, frozen["schema_ddl_hash"], 117, "a" * 64, _SCHEMA_STRUCTURE_FINGERPRINTS[116]
    )
    actual = {
        **frozen,
        "schema_version": 117,
        "schema_ddl_hash": pair.target_ddl,
        "event_head": {"sequence": 2, "root_hash": "f" * 64},
    }
    return actual, frozen, pair


def enable(monkeypatch: pytest.MonkeyPatch, pair: recovery.RecoverySchemaPair) -> None:
    monkeypatch.setitem(_SCHEMA_DDL_FINGERPRINTS, pair.target_version, pair.target_ddl)
    monkeypatch.setitem(_SCHEMA_STRUCTURE_FINGERPRINTS, pair.target_version, pair.structure)
    monkeypatch.setattr(recovery, "_REVIEWED_SCHEMA_PAIRS", (pair,))


def test_only_actual_reviewed_publication_migration_enables_cross_schema_recovery() -> None:
    assert len(recovery._REVIEWED_SCHEMA_PAIRS) == 1
    pair = recovery._REVIEWED_SCHEMA_PAIRS[0]
    assert (pair.source_version, pair.target_version) == (116, 117)
    assert pair.source_ddl == _SCHEMA_DDL_FINGERPRINTS[116]
    assert pair.target_ddl == _SCHEMA_DDL_FINGERPRINTS[117]
    assert pair.structure == _SCHEMA_STRUCTURE_FINGERPRINTS[116]
    assert pair.structure == _SCHEMA_STRUCTURE_FINGERPRINTS[117]


def test_source_comparison_preserves_actual_and_frozen_evidence(
    sources: tuple[dict[str, Any], dict[str, Any], recovery.RecoverySchemaPair],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual, frozen, pair = sources
    before = canonical_json([actual, frozen])
    assert not recovery.matches_publication_source(actual, frozen, existing_request=True)
    enable(monkeypatch, pair)
    assert recovery.matches_publication_source(actual, frozen, existing_request=True)
    assert canonical_json([actual, frozen]) == before
    assert actual["schema_version"] == 117 and frozen["schema_version"] == 116
    # The ordinary same-source comparison keeps the existing anchor-prefix contract.
    assert recovery.matches_publication_source(
        {**frozen, "event_head": actual["event_head"]}, frozen, existing_request=False
    )


@pytest.mark.parametrize("existing", (False, None, 1, "true"))
def test_a_new_request_or_truthy_non_boolean_cannot_use_the_bridge(
    sources: tuple[dict[str, Any], dict[str, Any], recovery.RecoverySchemaPair],
    monkeypatch: pytest.MonkeyPatch,
    existing: Any,
) -> None:
    actual, frozen, pair = sources
    enable(monkeypatch, pair)
    assert not recovery.matches_publication_source(actual, frozen, existing_request=existing)


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject_id", "another-subject"),
        ("workflow_id", "another-workflow"),
        ("genesis_hash", "0" * 64),
        ("records", 5),
        ("state_bytes", 9),
        ("history_hash", "0" * 64),
        ("receipt_count", 1),
        ("receipts_hash", "0" * 64),
        ("last_row_hash", "0" * 64),
        ("high_water", "2026-11-10T01:00:00+00:00"),
        ("schema_version", 118),
        ("schema_ddl_hash", "0" * 64),
    ],
)
def test_every_business_field_and_schema_pair_remains_exact(
    sources: tuple[dict[str, Any], dict[str, Any], recovery.RecoverySchemaPair],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    actual, frozen, pair = sources
    enable(monkeypatch, pair)
    assert not recovery.matches_publication_source(
        {**actual, field: value}, frozen, existing_request=True
    )
    # Same-schema history changes must remain rejected too.
    if field not in {"schema_version", "schema_ddl_hash"}:
        assert not recovery.matches_publication_source(
            {**frozen, field: value}, frozen, existing_request=True
        )


@pytest.mark.parametrize(
    "kind",
    (
        "missing_target",
        "changed_target",
        "changed_source",
        "missing_structure",
        "changed_structure",
        "reversed",
        "transitive",
    ),
)
def test_missing_or_revised_contracts_never_infer_compatibility(
    sources: tuple[dict[str, Any], dict[str, Any], recovery.RecoverySchemaPair],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    actual, frozen, pair = sources
    enable(monkeypatch, pair)
    if kind == "missing_target":
        monkeypatch.delitem(_SCHEMA_DDL_FINGERPRINTS, 117)
    elif kind == "changed_target":
        monkeypatch.setitem(_SCHEMA_DDL_FINGERPRINTS, 117, "0" * 64)
    elif kind == "changed_source":
        monkeypatch.setitem(_SCHEMA_DDL_FINGERPRINTS, 116, "0" * 64)
    elif kind == "missing_structure":
        monkeypatch.delitem(_SCHEMA_STRUCTURE_FINGERPRINTS, 117)
    elif kind == "changed_structure":
        monkeypatch.setitem(_SCHEMA_STRUCTURE_FINGERPRINTS, 117, "0" * 64)
    elif kind == "reversed":
        actual, frozen = frozen, actual
    else:
        monkeypatch.setitem(_SCHEMA_DDL_FINGERPRINTS, 118, "0" * 64)
        monkeypatch.setitem(_SCHEMA_STRUCTURE_FINGERPRINTS, 118, pair.structure)
        monkeypatch.setattr(
            recovery,
            "_REVIEWED_SCHEMA_PAIRS",
            (
                pair,
                replace(
                    pair,
                    source_version=117,
                    source_ddl=pair.target_ddl,
                    target_version=118,
                    target_ddl="0" * 64,
                ),
            ),
        )
        actual = {**actual, "schema_version": 118, "schema_ddl_hash": "0" * 64}
    assert not recovery.matches_publication_source(actual, frozen, existing_request=True)


@pytest.mark.parametrize(
    "change",
    (
        {"records": True},
        {"extra": "unsupported"},
        {"schema_version": 117.0},
        {"history_hash": "bad"},
    ),
)
def test_malformed_source_is_rejected_before_comparison(
    sources: tuple[dict[str, Any], dict[str, Any], recovery.RecoverySchemaPair],
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, Any],
) -> None:
    actual, frozen, pair = sources
    enable(monkeypatch, pair)
    with pytest.raises(IntegrityError):
        recovery.matches_publication_source({**actual, **change}, frozen, existing_request=True)


def test_schema115_does_not_inherit_approval_for_the116_pair(
    sources: tuple[dict[str, Any], dict[str, Any], recovery.RecoverySchemaPair],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual, frozen, pair = sources
    enable(monkeypatch, pair)
    legacy = {**frozen, "schema_version": 115, "schema_ddl_hash": _SCHEMA_DDL_FINGERPRINTS[115]}
    assert not recovery.matches_publication_source(actual, legacy, existing_request=True)
