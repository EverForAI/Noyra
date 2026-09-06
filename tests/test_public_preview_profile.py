from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import secrets
import socket
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from noyra.service import NoyraService

ROOT = Path(__file__).resolve().parents[1]


def read_env(relative: str) -> dict[str, str]:
    values = {}
    for line in (ROOT / relative).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator and key not in values
        values[key] = value
    return values


@pytest.mark.parametrize("relative", [".env.example", "deploy/noyra.env.example"])
def test_fresh_preview_has_no_external_economic_or_file_authority(
    tmp_path: Path, relative: str
) -> None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("NOYRA_")}
    example = read_env(relative)
    for key in (
        "NOYRA_COGNITION_ENABLED",
        "NOYRA_WALLET_AUTOMATION_ENABLED",
        "NOYRA_WALLET_AUTOMATION_AUTO_PUBLISH",
    ):
        assert example[key] == "false"
    for key, value in example.items():
        if key.endswith(
            ("_API_KEY", "_TOKEN", "_SIGNER_ENDPOINT", "_SIGNER_ID", "_ENCRYPTION_KEY")
        ):
            assert not value, key
    assert example["NOYRA_HOST"] == "127.0.0.1"
    environment.update(example)
    environment.update(
        {
            "NOYRA_DATA_DIR": str(tmp_path / "fresh-data"),
            "NOYRA_HOST": "127.0.0.1",
            "NOYRA_PORT": "0",
            "NOYRA_SUBJECT_ID": "Noyra-public-preview-test",
            "NOYRA_GENESIS_HASH": hashlib.sha256(b"public-preview-test").hexdigest(),
            "NOYRA_AT_REST_MODE": "development",
            "NOYRA_BACKUP_KEYRING_PATH": "",
            "NOYRA_VOLUME_ATTESTATION_PATH": "",
            "NOYRA_VOLUME_ENCRYPTION_BACKEND": "auto",
            "NOYRA_READ_TOKEN": secrets.token_urlsafe(32),
            "NOYRA_OPERATOR_TOKEN": secrets.token_urlsafe(32),
            "NOYRA_EXPORT_TOKEN": secrets.token_urlsafe(32),
            "NOYRA_BREAK_GLASS_TOKEN": secrets.token_urlsafe(32),
            "NOYRA_ARCHIVE_ENCRYPTION_KEY": base64.urlsafe_b64encode(
                secrets.token_bytes(32)
            ).decode(),
        }
    )
    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("outbound network is forbidden in preview check"),
        ) as connect,
    ):
        service = NoyraService.from_env()
        try:
            service.boot()
            assert service.http.wallet_signer is None
            assert service.http.wallet_execution is None
            assert service._wallet_automation_config is None
            assert service.cognition is None and service.cognition_gateway is None
            assert service.http.address[0] == "127.0.0.1"
            assert not service._boot_recovery_pending
            for table in (
                "capability_grants",
                "wallet_addresses",
                "wallet_payment_orders",
                "interaction_transports",
            ):
                with service.kernel.database.connection() as connection:
                    count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert count == 0, table
            assert connect.call_count == 0
        finally:
            service.close()


@pytest.mark.parametrize("version", [6, 16, 21, 27, 31])
def test_historical_fixtures_have_only_generated_content(tmp_path: Path, version: int) -> None:
    manifest = json.loads((ROOT / "tests/fixtures/historical/manifest.json").read_text())
    item = next(row for row in manifest["fixtures"] if row["schema_version"] == version)
    compressed = (ROOT / "tests/fixtures/historical" / item["archive"]).read_bytes()
    data = gzip.decompress(compressed)
    assert hashlib.sha256(compressed).hexdigest() == item["archive_sha256"]
    assert hashlib.sha256(data).hexdigest() == item["database_sha256"]
    path = tmp_path / "fixture.sqlite3"
    path.write_bytes(data)
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA freelist_count").fetchone()[0] == 0
        assert db.execute("SELECT subject_id,created_at FROM subject_identity").fetchall() == [
            (item["subject_id"], "2020-01-01T00:00:00.000+00:00")
        ]
        rows = db.execute("SELECT event_id,event_type,source,payload_json FROM events").fetchall()
        assert len(rows) == 1
        event = rows[0]
        assert event[:3] == (item["anchor_event_id"], "m41_historical_anchor", "m41-fixture")
        assert json.loads(event[3]) == {"fixture": item["name"], "schema_version": version}
        for (table,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall():
            if table in {
                "schema_meta",
                "subject_identity",
                "events",
                "memory_fts_data",
                "memory_fts_config",
            }:
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            assert db.execute("SELECT COUNT(*) FROM " + quoted).fetchone()[0] == 0, table
