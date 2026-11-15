"""Historical schema116/117 fixtures and the actual reviewed migrations."""

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

TARGET = 118


@pytest.fixture
def control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[object]:
    # Only fixture creation uses the historical runtime version. Recovery runs
    # the actual migrations with original rows and frozen contract fingerprints.
    version = getattr(request, "param", 116)
    assert version in (116, 117)
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
        assert version in (116, 117)
        assert Database.schema_ddl_fingerprint(snapshot) == _SCHEMA_DDL_FINGERPRINTS[version]
    return recovery.RecoverySchemaPair(
        version,
        _SCHEMA_DDL_FINGERPRINTS[version],
        TARGET,
        _SCHEMA_DDL_FINGERPRINTS[TARGET],
        _SCHEMA_STRUCTURE_FINGERPRINTS[version],
        _SCHEMA_STRUCTURE_FINGERPRINTS[TARGET],
    )


@contextmanager
def publication_upgrade_runtime(
    pair: recovery.RecoverySchemaPair, *, approved: bool = True
) -> Iterator[None]:
    # Contracts and migration are actual production definitions. Only negative
    # tests disable the explicitly reviewed source-recovery pair.
    assert pair in recovery._REVIEWED_SCHEMA_PAIRS
    if approved:
        yield
    else:
        with patch.object(recovery, "_REVIEWED_SCHEMA_PAIRS", ()):
            yield
