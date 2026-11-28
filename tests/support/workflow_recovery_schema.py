"""Fixtures for explicit historical workflow recovery migrations."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from noyra.core import Database
from noyra.core import database as database_module
from noyra.core import workflow_source_recovery as recovery
from noyra.core.schema.contracts import _SCHEMA_DDL_FINGERPRINTS, _SCHEMA_STRUCTURE_FINGERPRINTS
from test_d2b_workflow_export_control import control as current_control

TARGET_BY_SOURCE = {116: 118, 117: 118, 124: 125}


@pytest.fixture
def control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[object]:
    # Create the original rows at the source schema for the selected transition.
    version = getattr(request, "param", 116)
    assert version in TARGET_BY_SOURCE
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", version):
        controls = cast(Any, current_control).__wrapped__(tmp_path, monkeypatch)
        instance = next(controls)
    try:
        yield instance
    finally:
        controls.close()


def measured_pair(database: Database) -> recovery.RecoverySchemaPair:
    with database.read_snapshot() as snapshot:
        version = int(
            snapshot.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[
                0
            ]
        )
        target = TARGET_BY_SOURCE[version]
        assert Database.schema_ddl_fingerprint(snapshot) == _SCHEMA_DDL_FINGERPRINTS[version]
    return recovery.RecoverySchemaPair(
        version,
        _SCHEMA_DDL_FINGERPRINTS[version],
        target,
        _SCHEMA_DDL_FINGERPRINTS[target],
        _SCHEMA_STRUCTURE_FINGERPRINTS[version],
        _SCHEMA_STRUCTURE_FINGERPRINTS[target],
    )


@contextmanager
def publication_upgrade_runtime(
    pair: recovery.RecoverySchemaPair, *, approved: bool = True
) -> Iterator[None]:
    # Stop at the release under test while running the real migrations and
    # fingerprint checks. This also applies when a fresh process boots the DB.
    assert pair in recovery._REVIEWED_SCHEMA_PAIRS
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", pair.target_version):
        if approved:
            yield
        else:
            with patch.object(recovery, "_REVIEWED_SCHEMA_PAIRS", ()):
                yield
