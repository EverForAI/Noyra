"""Ephemeral workflow evidence view; never a runtime database or activation path."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from typing import Any

from .types import canonical_json, strict_json_loads
from .workflow_bundle import (
    MAX_CHUNK_BYTES,
    BundleReader,
    _bundle_records,
    _verified_records,
    require,
    validate_source,
)

MAX_RECONSTRUCTION_BYTES = 512 * 1024 * 1024
MAX_SQLITE_PAGES = 196_608  # 768 MiB, including indexes/SQLite overhead.
MAX_PAGE_BYTES = MAX_CHUNK_BYTES
MAX_PAGE_ROWS = 100


class _WorkflowEvidence:
    def __init__(
        self, connection: sqlite3.Connection, source: dict[str, Any], checkpoint: Callable[[], None]
    ) -> None:
        self._connection = connection
        self._source = source
        self._checkpoint = checkpoint
        self._closed = False

    def _ready(self) -> None:
        if self._closed:
            raise ValueError("workflow evidence view is closed")
        self._checkpoint()

    def summary(self) -> dict[str, Any]:
        self._ready()
        return {
            "format": "workflow-isolated-evidence/v1",
            "workflow_id": self._source["workflow_id"],
            "records": self._source["records"],
            "state_bytes": self._source["state_bytes"],
            "receipt_count": self._source["receipt_count"],
            "source_schema_version": self._source["schema_version"],
            "scope": self._source["scope"],
            "complete_history_verified": True,
            "isolated_reconstruction_verified": True,
            "storage": "ephemeral_memory_sqlite",
            "source_history_preserved": True,
            "restore_or_dispatch_authorized": False,
        }

    def page(self, *, after_version: int = 0, limit: int = 50) -> dict[str, Any]:
        self._ready()
        if (
            type(after_version) is not int
            or not 0 <= after_version <= self._source["records"]
            or type(limit) is not int
            or not 1 <= limit <= MAX_PAGE_ROWS
        ):
            raise ValueError("invalid workflow evidence page")
        records: list[dict[str, Any]] = []
        total = 0
        last = after_version
        for version, encoded in self._connection.execute(
            "SELECT version,record_json FROM reconstructed_records WHERE version>? "
            "ORDER BY version LIMIT ?",
            (after_version, limit),
        ):
            self._checkpoint()
            size = len(encoded.encode())
            if total + size > MAX_PAGE_BYTES:
                require(bool(records), "one reconstructed record exceeds page budget")
                break
            records.append(strict_json_loads(encoded))
            total += size
            last = version
        self._ready()
        return {
            "records": records,
            "next_after_version": last,
            "truncated": last < self._source["records"],
        }

    def latest(self) -> dict[str, Any]:
        self._ready()
        result = self.page(after_version=self._source["records"] - 1, limit=1)
        return dict(result["records"][0])


def _records(connection: sqlite3.Connection) -> Iterator[Any]:
    for (encoded,) in connection.execute(
        "SELECT record_json FROM reconstructed_records ORDER BY version"
    ):
        yield strict_json_loads(encoded)


@contextmanager
def _reconstruct(
    store: BundleReader,
    descriptor: dict[str, Any],
    expected: dict[str, Any],
    checkpoint: Callable[[], None],
) -> Iterator[_WorkflowEvidence]:
    """Internal: the caller supplies an independently validated accepted root.

    Nothing is exposed until both the file aggregate and the reconstructed SQL
    aggregate match that root. No path, live Database or runtime is a target.
    """
    validate_source(expected)
    source = strict_json_loads(canonical_json(expected))
    checkpoint()
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("PRAGMA page_size=4096")
        connection.execute(f"PRAGMA max_page_count={MAX_SQLITE_PAGES}")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA journal_mode=MEMORY")
        connection.execute(
            "CREATE TABLE reconstructed_records("
            "version INTEGER PRIMARY KEY,record_json TEXT NOT NULL)"
        )
        total = 0
        with (
            closing(_bundle_records(store, descriptor, source, checkpoint)) as candidates,
            closing(_verified_records(candidates, source, checkpoint)) as verified,
        ):
            for record in verified:
                encoded = canonical_json(record)
                total += len(encoded.encode())
                require(total <= MAX_RECONSTRUCTION_BYTES, "isolated reconstruction exceeds budget")
                connection.execute(
                    "INSERT INTO reconstructed_records VALUES(?,?)",
                    (record["row"]["checkpoint_version"], encoded),
                )
        connection.commit()
        connection.execute("PRAGMA query_only=ON")
        # Independent pass over what SQLite actually retained, not a cached
        # aggregate reported by the import loop or by the artifact itself.
        with closing(_verified_records(_records(connection), source, checkpoint)) as restored:
            for _ in restored:
                pass
        checkpoint()
        view = _WorkflowEvidence(connection, source, checkpoint)
        try:
            yield view
        finally:
            view._closed = True
