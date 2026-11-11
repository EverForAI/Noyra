"""Historical schema116 fixtures and the actual publication-guard migration."""

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

TARGET = 117


@pytest.fixture
def control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    # Use real historical rows and fingerprints; never relabel a schema117 DB.
    # Only creation runs as116. Tests exercise the actual migration after this.
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 116):
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
        assert version == 116
        assert Database.schema_ddl_fingerprint(snapshot) == _SCHEMA_DDL_FINGERPRINTS[116]
    return recovery.RecoverySchemaPair(
        116,
        _SCHEMA_DDL_FINGERPRINTS[116],
        TARGET,
        _SCHEMA_DDL_FINGERPRINTS[TARGET],
        _SCHEMA_STRUCTURE_FINGERPRINTS[116],
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
