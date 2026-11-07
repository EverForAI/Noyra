from __future__ import annotations

import base64
import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.autonomy.workflow import DurableWorkflowStore
from noyra.core import Database, EventStore, IdentityStore
from noyra.core.archive import ArchiveKeyring
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, strict_json_loads
from noyra.core.workflow_bundle import (
    ArrayDigest,
    inspect_source,
    source_identity,
    verify_bundle,
    write_bundle,
)
from noyra.core.workflow_export_files import WorkflowArtifactStore


@pytest.fixture(scope="module")
def template(tmp_path_factory: Any) -> Path:
    database = Database(tmp_path_factory.mktemp("schema") / "template.sqlite3")
    IdentityStore(database).ensure("export-subject", content_hash("export-genesis"))
    return database.path


@pytest.fixture
def database(template: Path, tmp_path: Path) -> Iterator[Database]:
    path = tmp_path / "subject.sqlite3"
    with closing(sqlite3.connect(template)) as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)
    yield Database(path, initialize=False)


def seed(database: Database, count: int) -> None:
    with database.transaction() as c:
        c.executemany(
            "INSERT INTO workflow_checkpoints VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    "workflow",
                    "export-subject",
                    "fixture",
                    version,
                    "running",
                    "{}",
                    content_hash({}),
                    "Private synthetic reason",
                    "2026-11-06T12:00:00.000+00:00",
                )
                for version in range(1, count + 1)
            ],
        )


class Objects:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def put(self, name: str, value: bytes) -> dict[str, Any]:
        self.values[name] = value
        return {"name": name, "hash": content_hash(value.hex())}

    def get(self, entry: dict[str, Any], *, maximum: int) -> bytes:
        value = self.values[entry["name"]]
        assert len(value) <= maximum
        if entry["hash"] != content_hash(value.hex()):
            raise IntegrityError("synthetic object changed")
        return value


def nothing() -> None:
    pass


def export(database: Database, store: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    with database.read_snapshot() as c:
        proof = inspect_source(c, database, "export-subject", "workflow", nothing)
        descriptor = write_bundle(c, database, store, proof, nothing)
    verify_bundle(store, descriptor, proof, nothing)
    return proof, descriptor


def test_stream_hash_matches_original_array() -> None:
    digest = ArrayDigest()
    assert digest.finish() == content_hash([])
    rows = [{"n": n, "text": "雪"} for n in range(50)]
    for row in rows:
        digest.add(row)
    assert digest.finish() == content_hash(rows)


def test_large_history_roundtrip_is_complete_and_never_writes_source(database: Database) -> None:
    seed(database, 50_001)
    with pytest.raises(ValueError, match="exceeds recovery bound"):
        DurableWorkflowStore(database).clock_history("export-subject", "workflow")
    objects = Objects()
    proof, descriptor = export(database, objects)
    with database.read_transaction() as c:
        rows = [
            dict(row)
            for row in c.execute("SELECT * FROM workflow_checkpoints ORDER BY checkpoint_version")
        ]
        assert proof["history_hash"] == content_hash(rows) and proof["records"] == 50_001
        assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert len(objects.values) > 2
    objects.values["segment-000001.enc"] = b"corrupt"
    with pytest.raises(IntegrityError):
        verify_bundle(objects, descriptor, proof, nothing)


def test_clock_receipts_survive_complete_readback(database: Database) -> None:
    store = DurableWorkflowStore(database)
    for index, hour in enumerate((12, 10, 11, 13), 1):
        with patch(
            "noyra.autonomy.workflow.utc_now", return_value=f"2026-11-06T{hour}:00:00.000+00:00"
        ):
            store.checkpoint(
                "export-subject", "workflow", "fixture", "running", {"n": index}, reason="clock"
            )
    proof, _ = export(database, Objects())
    assert proof["receipt_count"] == 2 and proof["high_water"] == "2026-11-06T13:00:00.000+00:00"
    EventStore(database).verify_chain("export-subject")


@pytest.mark.parametrize("damage", ["hash", "owner", "gap", "type"])
def test_source_corruption_cannot_be_exported_as_valid(database: Database, damage: str) -> None:
    seed(database, 4)
    IdentityStore(database).ensure("other", content_hash("other"))
    with database.transaction() as c:
        if damage == "hash":
            c.execute(
                "UPDATE workflow_checkpoints SET state_hash=? WHERE checkpoint_version=2",
                ("0" * 64,),
            )
        elif damage == "owner":
            c.execute(
                "UPDATE workflow_checkpoints SET subject_id='other' WHERE checkpoint_version=2"
            )
        elif damage == "gap":
            c.execute("DELETE FROM workflow_checkpoints WHERE checkpoint_version=2")
        else:
            c.execute(
                "UPDATE workflow_checkpoints SET workflow_type='other' WHERE checkpoint_version=2"
            )
    with pytest.raises(IntegrityError):
        export(database, Objects())


def test_budget_and_checkpoint_cancellation_stop_before_publication(database: Database) -> None:
    seed(database, 4)
    with patch("noyra.core.workflow_bundle.MAX_ROWS", 3), pytest.raises(IntegrityError):
        export(database, Objects())

    def cancelled() -> None:
        raise RuntimeError("synthetic invalidated operation")

    objects = Objects()
    with database.read_snapshot() as c, pytest.raises(RuntimeError, match="invalidated"):
        proof = inspect_source(c, database, "export-subject", "workflow", nothing)
        write_bundle(c, database, objects, proof, cancelled)
    assert "manifest.enc" not in objects.values


def test_encrypted_files_use_protected_key_and_readback_hashes(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    ring = ArchiveKeyring.from_env()
    seed(database, 5)
    files = WorkflowArtifactStore(
        database,
        "export-subject",
        "wexport_" + "a" * 64,
        ring,
        ring.active.key_id,
        ring.active.fingerprint,
        create=True,
    )
    proof, descriptor = export(database, files)
    assert proof["records"] == 5
    for path in files.root.iterdir():
        assert b"Private synthetic reason" not in path.read_bytes()
    files.permissions()
    manifest = files.root / (files.publication + "_manifest.enc")
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == descriptor["stored_hash"]
    value = manifest.read_bytes()
    manifest.write_bytes(value[:-1] + bytes([value[-1] ^ 1]))
    with pytest.raises(IntegrityError):
        verify_bundle(files, descriptor, proof, nothing)
    with pytest.raises(IntegrityError):
        files.get({**descriptor, "name": "../manifest.enc"}, maximum=1024 * 1024)


def test_source_identity_ignores_only_advancing_event_head(database: Database) -> None:
    seed(database, 3)
    first, _ = export(database, Objects())
    EventStore(database).append("export-subject", "unrelated", "fixture", {})
    second, _ = export(database, Objects())
    assert source_identity(first) == source_identity(second)
    assert first["event_head"] != second["event_head"]
    with database.transaction() as c:
        c.execute(
            "UPDATE workflow_checkpoints SET state_json=?,state_hash=? WHERE checkpoint_version=1",
            (canonical_json({"changed": True}), content_hash({"changed": True})),
        )
    changed, _ = export(database, Objects())
    assert changed["last_row_hash"] == first["last_row_hash"]
    assert source_identity(changed) != source_identity(first)


@pytest.mark.parametrize("invalid_count", [True, 1.0])
def test_manifest_source_preserves_exact_json_types(database: Database, invalid_count: Any) -> None:
    seed(database, 1)
    objects = Objects()
    proof, _ = export(database, objects)
    manifest = strict_json_loads(objects.values["manifest.enc"])
    manifest["source"]["records"] = invalid_count
    descriptor = objects.put("manifest.enc", canonical_json(manifest).encode())
    with pytest.raises(IntegrityError, match="manifest source mismatch"):
        verify_bundle(objects, descriptor, proof, nothing)
