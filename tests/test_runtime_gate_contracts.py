from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core.errors import RuntimeOwnershipError
from noyra.service import NoyraService
from test_gate0_runtime_boundaries import _settings


def business_state(connection: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        name: list(connection.execute("SELECT * FROM " + name + " ORDER BY rowid"))
        for name in (
            "subject_identity",
            "runtime_state",
            "state_snapshots",
            "events",
            "actions",
            "model_calls",
            "model_attempts",
            "audit_records",
        )
    }


def test_unowned_service_diagnostic_reads_committed_wal_without_business_writes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "runtime", "gate-contract-wal")
    owner = NoyraService(settings)
    observer: NoyraService | None = None
    try:
        owner.boot()
        database = settings.data_dir / "noyra.sqlite3"
        with closing(sqlite3.connect(database)) as anchor:
            assert anchor.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert anchor.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
            anchor.execute("PRAGMA wal_autocheckpoint=0")
            checkpoint = owner.kernel.checkpoint(
                {"state": "committed WAL only"}, reason="Synthetic diagnostic boundary"
            )
            paused = owner.kernel.pause("Synthetic WAL-only pause")
            wal = database.with_name(database.name + "-wal")
            before_bytes = (database.read_bytes(), wal.read_bytes())
            before = business_state(anchor)
            with patch(
                "noyra.core.database.sqlite3.connect",
                side_effect=AssertionError("unowned startup cannot open SQLite"),
            ):
                observer = NoyraService(settings)
                with pytest.raises(RuntimeOwnershipError):
                    observer.boot()
            assert not observer.kernel.process_lock.held and owner.kernel.process_lock.held
            assert observer.kernel.database.read_only
            assert not observer.kernel.database.immutable_snapshot
            health = observer.kernel.health()
            assert health["lifecycle"] == paused.__dict__
            assert observer.kernel.identity is not None
            assert observer.kernel.identity.last_checkpoint == checkpoint.snapshot_id
            assert (
                observer.kernel.snapshot_store.latest(settings.subject_id).snapshot_id
                == checkpoint.snapshot_id
            )
            # A write must still fail even when the additional query_only
            # safeguard is explicitly disabled: mode=ro is authoritative.
            with observer.kernel.database.connection() as reader:
                reader.execute("PRAGMA query_only=OFF")
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    reader.execute("UPDATE runtime_state SET version=version+1")
            assert business_state(anchor) == before
            assert (database.read_bytes(), wal.read_bytes()) == before_bytes
            observer.close()
            assert owner.kernel.process_lock.held
    finally:
        if observer is not None:
            observer.close()
        owner.close()
