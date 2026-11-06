"""Bounded complete workflow bundles; no source mutation or runtime restore."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from typing import Any, Protocol

from . import checkpoint_clock
from .database import Database
from .errors import IntegrityError
from .events import EventStore
from .types import canonical_json, content_hash, strict_json_loads

FORMAT = "noyra-workflow-export/v1"
SCOPE = "workflow_history_and_clock_receipts_only"
MAX_ROWS = 1_000_000
MAX_STATE_BYTES = 256 * 1024 * 1024
MAX_STATE_ROW_BYTES = 4 * 1024 * 1024
MAX_CHUNK_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SEGMENTS = 2048
CHUNK_ROWS = 256
CHUNK_TARGET_BYTES = 512 * 1024
ROW_FIELDS = frozenset(
    {
        "workflow_id",
        "subject_id",
        "workflow_type",
        "checkpoint_version",
        "status",
        "state_json",
        "state_hash",
        "reason",
        "created_at",
    }
)
EVENT_FIELDS = (
    "event_id",
    "subject_id",
    "event_type",
    "source",
    "privacy_level",
    "occurred_at",
    "payload_hash",
)


class BundleStore(Protocol):
    def put(self, name: str, value: bytes) -> dict[str, Any]: ...
    def get(self, entry: dict[str, Any], *, maximum: int) -> bytes: ...


class ArrayDigest:
    def __init__(self) -> None:
        self._hash = hashlib.sha256(b"[")
        self.count = 0

    def add(self, value: Any) -> None:
        if self.count:
            self._hash.update(b",")
        self._hash.update(canonical_json(value).encode())
        self.count += 1

    def finish(self) -> str:
        result = self._hash.copy()
        result.update(b"]")
        return result.hexdigest()


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise IntegrityError("workflow export " + reason)


def validate_source(source: Any) -> None:
    from .scheduling_clock import aware_time

    require(
        isinstance(source, dict)
        and set(source)
        == {
            "format",
            "scope",
            "subject_id",
            "workflow_id",
            "genesis_hash",
            "schema_version",
            "schema_ddl_hash",
            "records",
            "state_bytes",
            "history_hash",
            "receipt_count",
            "receipts_hash",
            "last_row_hash",
            "high_water",
            "event_head",
        },
        "source fields are invalid",
    )
    require(source["format"] == FORMAT and source["scope"] == SCOPE, "source format is invalid")
    require(
        all(
            isinstance(source[name], str) and source[name].strip() and len(source[name]) <= 256
            for name in ("subject_id", "workflow_id")
        ),
        "source scope is invalid",
    )
    require(
        all(
            isinstance(source[name], str)
            and len(source[name]) == 64
            and not set(source[name]) - set("0123456789abcdef")
            for name in (
                "genesis_hash",
                "schema_ddl_hash",
                "history_hash",
                "receipts_hash",
                "last_row_hash",
            )
        ),
        "source digest is invalid",
    )
    require(
        type(source["records"]) is int
        and 0 < source["records"] <= MAX_ROWS
        and type(source["state_bytes"]) is int
        and 0 <= source["state_bytes"] <= MAX_STATE_BYTES
        and type(source["receipt_count"]) is int
        and 0 <= source["receipt_count"] <= source["records"]
        and type(source["schema_version"]) is int
        and source["schema_version"] > 0,
        "source counts are invalid",
    )
    require(isinstance(source["high_water"], str), "source clock is invalid")
    aware_time(source["high_water"])
    head = source["event_head"]
    require(
        isinstance(head, dict)
        and set(head) == {"sequence", "root_hash"}
        and type(head["sequence"]) is int
        and head["sequence"] >= 0,
        "source event anchor is invalid",
    )
    require(
        (head["sequence"] == 0 and head["root_hash"] is None)
        or (
            head["sequence"] > 0
            and isinstance(head["root_hash"], str)
            and len(head["root_hash"]) == 64
            and not set(head["root_hash"]) - set("0123456789abcdef")
        ),
        "source event digest is invalid",
    )


def source_identity(source: dict[str, Any]) -> str:
    # Other events, including this export's preparation/audit, can advance the
    # chain without changing the frozen business history. Its original head
    # is checked separately as an immutable prefix anchor.
    return content_hash({key: value for key, value in source.items() if key != "event_head"})


def chain_head(connection: Any, subject: str, checkpoint: Callable[[], None]) -> dict[str, Any]:
    counts = connection.execute(
        "SELECT (SELECT COUNT(*) FROM events WHERE subject_id=?),"
        "(SELECT COUNT(*) FROM event_chain_roots WHERE subject_id=?)",
        (subject, subject),
    ).fetchone()
    require(counts[0] == counts[1], "event chain coverage mismatch")
    previous = None
    count = 0
    for count, row in enumerate(
        connection.execute(
            "SELECT r.sequence_number,r.event_id AS root_event_id,r.previous_root_hash,r.root_hash,"
            "e.subject_id,e.event_id,e.event_type,e.source,e.occurred_at,e.observed_at,"
            "e.payload_hash,e.privacy_level,e.causal_parent_ids_json "
            "FROM event_chain_roots r JOIN events e ON e.event_id=r.event_id "
            "AND e.subject_id=r.subject_id "
            "WHERE r.subject_id=? ORDER BY r.sequence_number",
            (subject,),
        ),
        start=1,
    ):
        checkpoint()
        require(
            type(row["sequence_number"]) is int
            and row["sequence_number"] == count
            and row["root_event_id"] == row["event_id"]
            and row["previous_root_hash"] == previous
            and row["root_hash"] == EventStore._chain_hash(row, count, previous),
            "event chain mismatch",
        )
        previous = str(row["root_hash"])
    require(count == counts[0], "event chain incomplete")
    return {"sequence": count, "root_hash": previous}


def check_anchor(connection: Any, subject: str, anchor: dict[str, Any]) -> None:
    if anchor["sequence"]:
        row = connection.execute(
            "SELECT root_hash FROM event_chain_roots WHERE subject_id=? AND sequence_number=?",
            (subject, anchor["sequence"]),
        ).fetchone()
        require(row is not None and row[0] == anchor["root_hash"], "event prefix changed")


def _history(
    connection: Any, database: Database, subject: str, workflow: str, checkpoint: Callable[[], None]
) -> Iterator[dict[str, Any]]:
    from noyra.autonomy.workflow import DurableWorkflowStore

    events = EventStore(database)
    previous = None
    high = None
    count = 0
    for count, original in enumerate(
        connection.execute(
            "SELECT * FROM workflow_checkpoints WHERE workflow_id=? ORDER BY checkpoint_version",
            (workflow,),
        ),
        start=1,
    ):
        checkpoint()
        row = dict(original)
        require(
            set(row) == ROW_FIELDS
            and row["subject_id"] == subject
            and row["workflow_id"] == workflow
            and row["checkpoint_version"] == count,
            "history ownership or version mismatch",
        )
        DurableWorkflowStore._from_row(row)
        require(
            row["status"]
            in DurableWorkflowStore._ALLOWED[None if previous is None else previous["status"]]
            and (previous is None or row["workflow_type"] == previous["workflow_type"]),
            "history transition mismatch",
        )
        high = checkpoint_clock.verify(connection, row, previous, high, database, events=events)
        payload = checkpoint_clock.receipt(connection, row, database, events=events)
        receipt = None
        if payload is not None:
            event = connection.execute(
                "SELECT * FROM events WHERE event_id=?",
                (checkpoint_clock.receipt_id(subject, workflow, count),),
            ).fetchone()
            receipt = {**{key: event[key] for key in EVENT_FIELDS}, "payload": payload}
        yield {"row": row, "receipt": receipt, "high_water": high}
        previous = row
    require(count > 0, "history missing")


def inspect_source(
    connection: Any, database: Database, subject: str, workflow: str, checkpoint: Callable[[], None]
) -> dict[str, Any]:
    checkpoint()
    database._validate_schema_contract_connection(connection, strict=True, persist_marker=False)
    size = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(length(CAST(state_json AS BLOB))),0),"
        "COALESCE(MAX(length(CAST(state_json AS BLOB))),0) "
        "FROM workflow_checkpoints WHERE workflow_id=?",
        (workflow,),
    ).fetchone()
    require(
        0 < size[0] <= MAX_ROWS and size[1] <= MAX_STATE_BYTES and size[2] <= MAX_STATE_ROW_BYTES,
        "source exceeds export budget",
    )
    identity = connection.execute(
        "SELECT genesis_hash FROM subject_identity WHERE subject_id=?", (subject,)
    ).fetchone()
    require(identity is not None, "subject is missing")
    version = int(
        connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
    )
    head = chain_head(connection, subject, checkpoint)
    rows, receipts = ArrayDigest(), ArrayDigest()
    state_bytes = 0
    last: dict[str, Any] = {}
    for record in _history(connection, database, subject, workflow, checkpoint):
        row = record["row"]
        rows.add(row)
        state_bytes += len(row["state_json"].encode())
        if record["receipt"] is not None:
            receipts.add({"version": row["checkpoint_version"], "receipt": record["receipt"]})
        last = record
    require(rows.count == size[0] and state_bytes == size[1], "source size changed")
    return {
        "format": FORMAT,
        "scope": SCOPE,
        "subject_id": subject,
        "workflow_id": workflow,
        "genesis_hash": identity["genesis_hash"],
        "schema_version": version,
        "schema_ddl_hash": Database.schema_ddl_fingerprint(connection),
        "records": rows.count,
        "state_bytes": state_bytes,
        "history_hash": rows.finish(),
        "receipt_count": receipts.count,
        "receipts_hash": receipts.finish(),
        "last_row_hash": content_hash(last["row"]),
        "high_water": last["high_water"],
        "event_head": head,
    }


def write_bundle(
    connection: Any,
    database: Database,
    store: BundleStore,
    source: dict[str, Any],
    checkpoint: Callable[[], None],
) -> dict[str, Any]:
    chunks: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    byte_count = 0
    count = 0

    def flush() -> None:
        nonlocal pending, byte_count
        if not pending:
            return
        checkpoint()
        require(len(chunks) < MAX_SEGMENTS, "too many segments")
        encoded = canonical_json({"format": FORMAT, "records": pending}).encode()
        require(len(encoded) <= MAX_CHUNK_BYTES, "record exceeds chunk budget")
        item = store.put(f"segment-{len(chunks):06d}.enc", encoded)
        chunks.append(
            {
                **item,
                "records": len(pending),
                "first": pending[0]["row"]["checkpoint_version"],
                "last": pending[-1]["row"]["checkpoint_version"],
            }
        )
        pending, byte_count = [], 0

    for record in _history(
        connection, database, source["subject_id"], source["workflow_id"], checkpoint
    ):
        size = len(canonical_json(record).encode())
        require(size <= MAX_CHUNK_BYTES - 1024, "record exceeds chunk budget")
        if pending and (len(pending) >= CHUNK_ROWS or byte_count + size > CHUNK_TARGET_BYTES):
            flush()
        pending.append(record)
        count += 1
        byte_count += size
    flush()
    require(count == source["records"], "written count differs")
    manifest = canonical_json({"format": FORMAT, "source": source, "segments": chunks}).encode()
    require(len(manifest) <= MAX_MANIFEST_BYTES, "manifest exceeds budget")
    checkpoint()
    return store.put("manifest.enc", manifest)


def verify_bundle(
    store: BundleStore,
    descriptor: dict[str, Any],
    expected: dict[str, Any],
    checkpoint: Callable[[], None],
) -> None:
    """Read back all bounded chunks and clock receipts; never trusts self-declared source."""
    from noyra.autonomy.workflow import DurableWorkflowStore

    validate_source(expected)
    manifest = strict_json_loads(store.get(descriptor, maximum=MAX_MANIFEST_BYTES))
    require(
        isinstance(manifest, dict)
        and set(manifest) == {"format", "source", "segments"}
        and manifest["format"] == FORMAT
        and canonical_json(manifest["source"]) == canonical_json(expected),
        "manifest source mismatch",
    )
    segments = manifest["segments"]
    require(
        isinstance(segments, list) and 0 < len(segments) <= MAX_SEGMENTS, "invalid segment list"
    )
    rows, receipts = ArrayDigest(), ArrayDigest()
    previous = None
    high = None
    state_bytes = 0
    with closing(sqlite3.connect(":memory:")) as evidence:
        evidence.row_factory = sqlite3.Row
        evidence.execute(
            "CREATE TABLE events(event_id TEXT,subject_id TEXT,event_type TEXT,source TEXT,"
            "privacy_level TEXT,occurred_at TEXT,payload_json TEXT,"
            "payload_hash TEXT,payload_archive_key TEXT)"
        )
        for index, segment in enumerate(segments):
            checkpoint()
            require(
                isinstance(segment, dict) and segment.get("name") == f"segment-{index:06d}.enc",
                "segment name or sequence mismatch",
            )
            body = strict_json_loads(store.get(segment, maximum=MAX_CHUNK_BYTES))
            require(
                isinstance(body, dict)
                and set(body) == {"format", "records"}
                and body["format"] == FORMAT
                and isinstance(body["records"], list)
                and 0 < len(body["records"]) <= CHUNK_ROWS,
                "invalid chunk format",
            )
            require(
                type(segment.get("records")) is int
                and segment["records"] == len(body["records"])
                and segment.get("first") == rows.count + 1
                and segment.get("last") == rows.count + len(body["records"]),
                "invalid segment range",
            )
            for record in body["records"]:
                checkpoint()
                require(
                    isinstance(record, dict) and set(record) == {"row", "receipt", "high_water"},
                    "invalid chunk record",
                )
                row, receipt = record["row"], record["receipt"]
                require(
                    isinstance(row, dict)
                    and set(row) == ROW_FIELDS
                    and row["subject_id"] == expected["subject_id"]
                    and row["workflow_id"] == expected["workflow_id"]
                    and row["checkpoint_version"] == rows.count + 1,
                    "restored scope/version mismatch",
                )
                require(
                    isinstance(row["state_json"], str)
                    and len(row["state_json"].encode()) <= MAX_STATE_ROW_BYTES,
                    "restored row exceeds byte budget",
                )
                DurableWorkflowStore._from_row(row)
                require(
                    row["status"]
                    in DurableWorkflowStore._ALLOWED[
                        None if previous is None else previous["status"]
                    ]
                    and (previous is None or row["workflow_type"] == previous["workflow_type"]),
                    "restored transition mismatch",
                )
                evidence.execute("DELETE FROM events")
                if receipt is not None:
                    require(
                        isinstance(receipt, dict)
                        and set(receipt) == {*EVENT_FIELDS, "payload"}
                        and receipt["event_id"]
                        == checkpoint_clock.receipt_id(
                            expected["subject_id"], expected["workflow_id"], rows.count + 1
                        )
                        and receipt["payload_hash"] == content_hash(receipt["payload"]),
                        "invalid restored receipt",
                    )
                    evidence.execute(
                        "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,NULL)",
                        (
                            *(receipt[key] for key in EVENT_FIELDS[:-1]),
                            canonical_json(receipt["payload"]),
                            receipt["payload_hash"],
                        ),
                    )
                    receipts.add({"version": row["checkpoint_version"], "receipt": receipt})
                high = checkpoint_clock.verify(evidence, row, previous, high)
                require(high == record["high_water"], "restored clock differs")
                rows.add(row)
                state_bytes += len(row["state_json"].encode())
                require(
                    rows.count <= MAX_ROWS and state_bytes <= MAX_STATE_BYTES,
                    "restored budget exceeded",
                )
                previous = row
    require(
        previous is not None
        and rows.count == expected["records"]
        and state_bytes == expected["state_bytes"]
        and rows.finish() == expected["history_hash"]
        and receipts.count == expected["receipt_count"]
        and receipts.finish() == expected["receipts_hash"]
        and high == expected["high_water"]
        and content_hash(previous) == expected["last_row_hash"],
        "complete readback differs from source",
    )
