from __future__ import annotations

import base64
from typing import Any

import pytest

from noyra.core.archive import ArchiveKeyring
from noyra.core.types import content_hash
from noyra.core.workflow_bundle import inspect_source
from noyra.core.workflow_export_catalog import WorkflowExportCatalog
from test_d2b_workflow_bundle import database as database
from test_d2b_workflow_bundle import nothing, seed
from test_d2b_workflow_bundle import template as template


@pytest.mark.parametrize("durable", [False, True])
def test_catalog_binds_the_frozen_source_and_atomic_terminal_receipt(
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
    durable: bool,
) -> None:
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    seed(database, 8)
    ring = ArchiveKeyring.from_env()
    ring.record_revision(database, "export-subject")
    with database.read_snapshot() as c:
        source = inspect_source(c, database, "export-subject", "workflow", nothing)
    catalog = WorkflowExportCatalog(database, "export-subject")
    with database.transaction() as c:
        prepared = catalog.prepare(c, source, content_hash("request"), ring)
    assert prepared["status"] == "prepared"
    descriptor = {
        "name": "manifest.enc",
        "stored_bytes": 137,
        "stored_hash": "a" * 64,
        "plaintext_hash": "b" * 64,
    }
    with database.transaction() as c:
        final = catalog.finish(c, prepared, descriptor, durable=durable)
    assert final["status"] == ("accepted" if durable else "published_durability_unknown")
    with database.read_transaction() as c:
        restored = catalog.requested(c, content_hash("request"), "workflow", 8)
        assert restored == final
        assert c.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='workflow_export_accepted'"
        ).fetchone()[0] == int(durable)
        assert c.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0] == 8
        assert c.total_changes == 0
    assert catalog.page()["items"][0]["scope"] == "workflow_history_and_clock_receipts_only"
    assert catalog.page()["items"][0]["artifact_observation"] == "not_checked_by_catalog_read"
