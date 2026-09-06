from __future__ import annotations

import zlib
from collections.abc import Callable
from typing import Any

from .database import Database
from .errors import IntegrityError, NotFoundError
from .types import (
    SnapshotRecord,
    canonical_json,
    content_hash,
    new_id,
    strict_int,
    strict_json_loads,
    utc_now,
)


class _CompactionConflict(Exception):
    """A selected snapshot changed before its archive could be published."""


class SnapshotStore:
    def __init__(self, database: Database):
        self.database = database

    def save(
        self,
        subject_id: str,
        state: dict[str, Any],
        *,
        state_version: int,
        reason: str,
        snapshot_id: str | None = None,
    ) -> SnapshotRecord:
        with self.database.transaction() as connection:
            return self._save_connection(
                connection,
                subject_id,
                state,
                state_version=state_version,
                reason=reason,
                snapshot_id=snapshot_id,
            )

    def _save_connection(
        self,
        connection: Any,
        subject_id: str,
        state: dict[str, Any],
        *,
        state_version: int,
        reason: str,
        snapshot_id: str | None,
    ) -> SnapshotRecord:
        if state_version <= 0:
            raise ValueError("state_version must be positive")
        if not reason.strip():
            raise ValueError("snapshot reason is required")
        if not isinstance(state, dict):
            raise TypeError("snapshot state must be a dictionary")
        snapshot_id = snapshot_id or new_id("snap")
        created_at = utc_now()
        state_hash = content_hash(state)
        connection.execute(
            """INSERT INTO state_snapshots(
                snapshot_id, subject_id, state_version, state_json, state_hash, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                subject_id,
                state_version,
                canonical_json(state),
                state_hash,
                reason,
                created_at,
            ),
        )
        return SnapshotRecord(
            snapshot_id, subject_id, state_version, state, state_hash, reason, created_at
        )

    def latest(self, subject_id: str) -> SnapshotRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM state_snapshots WHERE subject_id = ? "
                "ORDER BY state_version DESC, created_at DESC, snapshot_id DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"snapshot not found for subject: {subject_id}")
        return self._from_row(row)

    def compact(
        self,
        subject_id: str,
        *,
        keep_recent: int = 16,
        batch_size: int = 256,
        max_batch_bytes: int = 8_000_000,
        checkpoint: Callable[[], None] | None = None,
        max_rows: int | None = None,
        max_bytes: int | None = None,
        row_budget: int | None = None,
        byte_budget: int | None = None,
        max_rows_per_batch: int | None = None,
        max_bytes_per_batch: int | None = None,
    ) -> dict[str, int]:
        """Compact old snapshots in bounded, retryable transactions.

        Snapshot JSON is read and compressed outside the write transaction.  Each
        bounded batch is then published with a compare-and-swap delete: if any
        selected row disappeared or changed while the archive was being built,
        the archive insert and delete are rolled back and the keyset is retried.
        ``max_rows``/``max_bytes`` (and their ``*_budget`` aliases) limit total
        work for a maintenance tick; the defaults preserve the historical
        behaviour of compacting all eligible history in multiple small batches.
        """
        if max_rows_per_batch is not None:
            batch_size = max_rows_per_batch
        if max_bytes_per_batch is not None:
            max_batch_bytes = max_bytes_per_batch
        if row_budget is not None:
            if max_rows is not None and max_rows != row_budget:
                raise ValueError("max_rows and row_budget disagree")
            max_rows = row_budget
        if byte_budget is not None:
            if max_bytes is not None and max_bytes != byte_budget:
                raise ValueError("max_bytes and byte_budget disagree")
            max_bytes = byte_budget
        bounded_keep = max(2, min(keep_recent, 1_000))
        bounded_batch = max(1, min(batch_size, 10_000))
        bounded_batch_bytes = max(1, min(max_batch_bytes, 128_000_000))
        bounded_rows = None if max_rows is None else max(0, max_rows)
        bounded_bytes = None if max_bytes is None else max(0, max_bytes)
        if bounded_rows == 0 or bounded_bytes == 0:
            return {"archived": 0, "compressed_bytes": 0}

        archived = 0
        compressed_bytes = 0
        consumed_bytes = 0
        cursor: tuple[int, str] | None = None
        conflicts_without_progress = 0
        while True:
            self._checkpoint(checkpoint)
            remaining_rows = (
                bounded_batch
                if bounded_rows is None
                else min(bounded_batch, bounded_rows - archived)
            )
            remaining_bytes = (
                bounded_batch_bytes
                if bounded_bytes is None
                else min(bounded_batch_bytes, bounded_bytes - consumed_bytes)
            )
            if remaining_rows <= 0 or remaining_bytes <= 0:
                break
            rows = self._read_compaction_batch(
                subject_id,
                keep_recent=bounded_keep,
                cursor=cursor,
                row_limit=remaining_rows,
                byte_limit=remaining_bytes,
                checkpoint=checkpoint,
            )
            if not rows:
                break
            self._checkpoint(checkpoint)
            archive_payload, raw_bytes = self._build_archive_payload(rows, checkpoint)
            if bounded_bytes is not None and consumed_bytes + raw_bytes > bounded_bytes:
                break
            compressed = zlib.compress(
                canonical_json(archive_payload).encode("utf-8"),
                level=9,
            )
            self._checkpoint(checkpoint)
            try:
                self._publish_compaction_batch(
                    subject_id,
                    rows,
                    archive_payload,
                    compressed,
                    checkpoint=checkpoint,
                )
            except _CompactionConflict:
                # A concurrent compactor may have won the CAS.  Do not advance
                # the keyset; reread it and let the winner's deletion move the
                # cursor forward.  A bounded retry guard prevents starvation
                # under a hostile writer/compactor storm.
                conflicts_without_progress += 1
                if conflicts_without_progress >= 8:
                    break
                continue
            conflicts_without_progress = 0
            archived += len(rows)
            consumed_bytes += raw_bytes
            compressed_bytes += len(compressed)
            last = rows[-1]
            cursor = (int(last["state_version"]), str(last["snapshot_id"]))
            self._checkpoint(checkpoint)
        return {"archived": archived, "compressed_bytes": compressed_bytes}

    def _read_compaction_batch(
        self,
        subject_id: str,
        *,
        keep_recent: int,
        cursor: tuple[int, str] | None,
        row_limit: int,
        byte_limit: int,
        checkpoint: Callable[[], None] | None,
    ) -> list[dict[str, Any]]:
        """Read one ascending keyset page without holding a write transaction."""
        with self.database.connection() as connection:
            recent = connection.execute(
                "SELECT state_version, snapshot_id FROM state_snapshots "
                "WHERE subject_id = ? ORDER BY state_version DESC, snapshot_id DESC LIMIT ?",
                (subject_id, keep_recent + 1),
            ).fetchall()
            if len(recent) <= keep_recent:
                return []
            # The oldest row in the retained tail is the boundary; the extra
            # row only proves that at least one older snapshot exists.
            cutoff = recent[keep_recent - 1]
            conditions = [
                "subject_id = ?",
                "(state_version < ? OR (state_version = ? AND snapshot_id < ?))",
            ]
            parameters: list[Any] = [
                subject_id,
                int(cutoff["state_version"]),
                int(cutoff["state_version"]),
                str(cutoff["snapshot_id"]),
            ]
            if cursor is not None:
                conditions.append("(state_version > ? OR (state_version = ? AND snapshot_id > ?))")
                parameters.extend((cursor[0], cursor[0], cursor[1]))
            sql = (
                "SELECT snapshot_id, subject_id, state_version, state_hash, reason, "
                "created_at, length(CAST(state_json AS BLOB)) AS state_bytes "
                "FROM state_snapshots WHERE "
                + " AND ".join(conditions)
                + " ORDER BY state_version ASC, snapshot_id ASC LIMIT ?"
            )
            parameters.append(row_limit)
            result: list[dict[str, Any]] = []
            materialized_bytes = 0
            rows = connection.execute(sql, tuple(parameters))
            while True:
                row = rows.fetchone()
                if row is None:
                    break
                self._checkpoint(checkpoint)
                # Check the SQLite-side byte length before selecting the TEXT
                # value into Python.  This keeps an oversized first row from
                # defeating the caller's hard compaction budget merely by
                # materializing a giant JSON string in the process.
                try:
                    row_bytes = int(row["state_bytes"])
                except (TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"snapshot state byte length is invalid: {row['snapshot_id']}"
                    ) from error
                if result and materialized_bytes + row_bytes > byte_limit:
                    break
                if not result and row_bytes > byte_limit:
                    # The per-batch byte budget is a hard admission limit even
                    # when no total tick budget was supplied.  Do not select
                    # the TEXT value into Python merely to discover that the
                    # batch cannot admit it.  Callers can retry with a larger
                    # explicit batch budget when a snapshot is intentionally
                    # larger than the normal maintenance bound.
                    break
                full = connection.execute(
                    "SELECT state_json, length(CAST(state_json AS BLOB)) AS state_bytes "
                    "FROM state_snapshots WHERE snapshot_id = ? "
                    "AND subject_id = ?",
                    (row["snapshot_id"], subject_id),
                ).fetchone()
                if full is None or not isinstance(full["state_json"], str):
                    raise IntegrityError(f"snapshot state JSON must be text: {row['snapshot_id']}")
                raw_state = full["state_json"]
                try:
                    observed_bytes = int(full["state_bytes"])
                except (TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"snapshot state byte length is invalid: {row['snapshot_id']}"
                    ) from error
                if observed_bytes != row_bytes:
                    raise IntegrityError(
                        f"snapshot state changed during bounded read: {row['snapshot_id']}"
                    )
                result.append(
                    {
                        "snapshot_id": str(row["snapshot_id"]),
                        "subject_id": str(row["subject_id"]),
                        "state_version": int(row["state_version"]),
                        "state_json": raw_state,
                        "state_bytes": row_bytes,
                        "state_hash": str(row["state_hash"]),
                        "reason": str(row["reason"]),
                        "created_at": str(row["created_at"]),
                    }
                )
                materialized_bytes += row_bytes
                if len(result) >= row_limit:
                    break
            return result

    def _build_archive_payload(
        self,
        rows: list[dict[str, Any]],
        checkpoint: Callable[[], None] | None,
    ) -> tuple[dict[str, Any], int]:
        entries: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        raw_bytes = 0
        for row in rows:
            self._checkpoint(checkpoint)
            state = strict_json_loads(row["state_json"])
            if not isinstance(state, dict):
                raise IntegrityError(f"snapshot state is not an object: {row['snapshot_id']}")
            if content_hash(state) != row["state_hash"]:
                raise IntegrityError(f"snapshot state hash mismatch: {row['snapshot_id']}")
            raw_bytes += int(row.get("state_bytes", len(row["state_json"].encode("utf-8"))))
            entry: dict[str, Any] = {
                "snapshot_id": row["snapshot_id"],
                "state_version": row["state_version"],
                "state_hash": row["state_hash"],
                "reason": row["reason"],
                "created_at": row["created_at"],
            }
            if previous is None:
                entry["base_state"] = state
            else:
                entry["delta"] = self._delta(previous, state)
            entries.append(entry)
            previous = state
        return {"format": "noyra-incremental-snapshots-v1", "entries": entries}, raw_bytes

    def _publish_compaction_batch(
        self,
        subject_id: str,
        rows: list[dict[str, Any]],
        archive_payload: dict[str, Any],
        compressed: bytes,
        *,
        checkpoint: Callable[[], None] | None,
    ) -> None:
        """Publish one archive and delete exactly the rows it covered (CAS)."""
        ids = [str(row["snapshot_id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        # Check before acquiring the write lock.  A caller's checkpoint may
        # inspect SQLite through another connection; invoking it while this
        # transaction is open could self-deadlock behind BEGIN IMMEDIATE.
        self._checkpoint(checkpoint)
        with self.database.transaction() as connection:
            for row in rows:
                current = connection.execute(
                    "SELECT subject_id, state_version, state_json, state_hash, reason, created_at "
                    "FROM state_snapshots WHERE snapshot_id = ? AND subject_id = ?",
                    (row["snapshot_id"], subject_id),
                ).fetchone()
                if current is None or (
                    str(current["subject_id"]) != subject_id
                    or int(current["state_version"]) != int(row["state_version"])
                    or str(current["state_hash"]) != str(row["state_hash"])
                    or str(current["state_json"]) != str(row["state_json"])
                    or str(current["reason"]) != str(row["reason"])
                    or str(current["created_at"]) != str(row["created_at"])
                ):
                    raise _CompactionConflict()
            archive_id = new_id("snapshot-archive")
            entries = archive_payload["entries"]
            assert isinstance(entries, list)
            connection.execute(
                """INSERT INTO snapshot_archives(
                    archive_id, subject_id, first_version, last_version, snapshot_count,
                    compressed_payload, payload_hash, compressed_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    archive_id,
                    subject_id,
                    int(rows[0]["state_version"]),
                    int(rows[-1]["state_version"]),
                    len(rows),
                    compressed,
                    content_hash(archive_payload),
                    content_hash({"compressed_hex": compressed.hex()}),
                    utc_now(),
                ),
            )
            deleted = connection.execute(
                f"DELETE FROM state_snapshots WHERE subject_id = ? "
                f"AND snapshot_id IN ({placeholders})",
                (subject_id, *ids),
            )
            if deleted.rowcount != len(rows):
                raise _CompactionConflict()
        self._checkpoint(checkpoint)

    @staticmethod
    def _checkpoint(checkpoint: Callable[[], None] | None) -> None:
        if checkpoint is not None:
            checkpoint()

    def verify_archives(self, subject_id: str) -> int:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM snapshot_archives WHERE subject_id = ?", (subject_id,)
            ).fetchall()
        for row in rows:
            compressed = bytes(row["compressed_payload"])
            if content_hash({"compressed_hex": compressed.hex()}) != row["compressed_hash"]:
                raise IntegrityError("snapshot archive compressed hash mismatch")
            payload = strict_json_loads(zlib.decompress(compressed))
            entries = payload.get("entries") if isinstance(payload, dict) else None
            if not isinstance(entries, list) or (
                content_hash(payload) != row["payload_hash"]
                or len(entries) != row["snapshot_count"]
            ):
                raise IntegrityError("snapshot archive payload mismatch")
            state: dict[str, Any] | None = None
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    raise IntegrityError("snapshot archive entry is invalid")
                if index == 0:
                    base = entry.get("base_state")
                    if not isinstance(base, dict):
                        raise IntegrityError("snapshot archive base state is invalid")
                    state = base
                else:
                    assert state is not None
                    state = self._apply_delta(state, entry.get("delta"))
                if content_hash(state) != entry.get("state_hash"):
                    raise IntegrityError("snapshot archive state hash mismatch")
        return len(rows)

    @staticmethod
    def _delta(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        return {
            # ``dict.get`` cannot distinguish a missing key from a present
            # key whose value is ``None``.  Preserve that distinction or a
            # valid snapshot transition would silently lose a newly-added
            # null-valued field during archive verification.
            "set": {
                key: value
                for key, value in current.items()
                if key not in previous or previous[key] != value
            },
            "delete": sorted(key for key in previous if key not in current),
        }

    @staticmethod
    def _apply_delta(state: dict[str, Any], raw: Any) -> dict[str, Any]:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("set"), dict)
            or not isinstance(raw.get("delete"), list)
        ):
            raise IntegrityError("snapshot archive delta is invalid")
        result = dict(state)
        for key in raw["delete"]:
            if not isinstance(key, str):
                raise IntegrityError("snapshot archive delete key is invalid")
            result.pop(key, None)
        result.update(raw["set"])
        return result

    @staticmethod
    def _from_row(row: Any) -> SnapshotRecord:
        try:
            if not isinstance(row["state_json"], str):
                raise TypeError("snapshot state JSON must be text")
            state = strict_json_loads(row["state_json"])
            state_version = strict_int(row["state_version"])
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(
                f"snapshot durable state is invalid: {row['snapshot_id']}"
            ) from error
        if not isinstance(state, dict):
            raise IntegrityError(f"snapshot state is not an object: {row['snapshot_id']}")
        state_hash = content_hash(state)
        if state_hash != row["state_hash"]:
            raise IntegrityError(f"snapshot state hash mismatch: {row['snapshot_id']}")
        return SnapshotRecord(
            snapshot_id=row["snapshot_id"],
            subject_id=row["subject_id"],
            state_version=state_version,
            state=state,
            state_hash=state_hash,
            reason=row["reason"],
            created_at=row["created_at"],
        )
