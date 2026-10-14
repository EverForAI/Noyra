from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path
from typing import Any

import pytest

from noyra.core.database import Database
from noyra.core.types import utc_now
from test_m42_p1_02_integrity_runtime import _active_kernel

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "diagnose-action-integrity.py"


def _diagnose(path: Path) -> tuple[dict[str, Any], int]:
    functions = runpy.run_path(str(SCRIPT))
    result: tuple[dict[str, Any], int] = functions["diagnose"](path)
    return result


def test_action_diagnostic_accepts_legacy_management_records_without_initializing_or_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = _active_kernel(tmp_path, "Noyra-readonly-action-diagnostic")
    try:
        with kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
                (
                    "audit_legacy_discovery",
                    kernel.subject_id,
                    "model_resource_discovery_succeeded",
                    "web-operator",
                    json.dumps({"model_count": 2}, sort_keys=True),
                    utc_now(),
                ),
            )
        before = hashlib.sha256(kernel.database.path.read_bytes()).hexdigest()
        with kernel.database.connection() as connection:
            records = [tuple(row) for row in connection.execute("SELECT * FROM audit_records")]

        def forbidden_initialize(_database: Database) -> None:
            raise AssertionError("a read-only diagnostic must never initialize or migrate")

        monkeypatch.setattr(Database, "initialize", forbidden_initialize)
        report, status = _diagnose(kernel.database.path)
        assert status == 0
        assert report["status"] == "ok"
        assert report["counts"]["audit_records"] == 1
        assert hashlib.sha256(kernel.database.path.read_bytes()).hexdigest() == before
        with kernel.database.connection() as connection:
            after = [tuple(row) for row in connection.execute("SELECT * FROM audit_records")]
        assert after == records
    finally:
        kernel.close()


def test_action_diagnostic_does_not_create_missing_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    report, status = _diagnose(path)
    assert status == 2
    assert report["reason"] == "missing_database"
    assert not path.exists()


def test_action_diagnostic_projects_failure_sites_without_private_payload(tmp_path: Path) -> None:
    kernel = _active_kernel(tmp_path, "Noyra-private-action-diagnostic")
    try:
        secret = "private-raw-content-that-must-not-leak"
        with kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
                (
                    "audit_bad_discovery",
                    kernel.subject_id,
                    "model_resource_discovery_succeeded",
                    "web-operator",
                    json.dumps({"model_count": secret}),
                    utc_now(),
                ),
            )
        report, status = _diagnose(kernel.database.path)
        assert status == 1
        assert report["status"] == "corrupt"
        assert secret not in json.dumps(report)
        assert any(site["function"] == "_provenance_int" for site in report["sites"])
    finally:
        kernel.close()
