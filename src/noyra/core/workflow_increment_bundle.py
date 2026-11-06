"""Bounded incremental encoding and complete writer readback; no public cold reader."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator, Iterator
from contextlib import closing
from itertools import islice
from typing import Any, cast

from .archive import ArchiveKeyring
from .database import Database
from .types import canonical_json, content_hash, strict_json_loads
from .workflow_bundle import (
    CHUNK_ROWS,
    CHUNK_TARGET_BYTES,
    MAX_CHUNK_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_SEGMENTS,
    _bundle_records,
    _history,
    _verified_records,
    check_anchor,
    require,
)
from .workflow_export_catalog import WorkflowExportCatalog
from .workflow_export_files import PUBLICATION, WorkflowArtifactStore
from .workflow_increment_catalog import WorkflowIncrementCatalog
from .workflow_increment_contract import FORMAT, MAX_CHAIN_NODES, compatible
from .workflow_increment_files import WorkflowIncrementStore
from .workflow_reconstruction import MAX_RECONSTRUCTION_BYTES, MAX_SQLITE_PAGES

MAX_CHAIN_SEGMENTS = 4096
Chain = list[tuple[str, dict[str, Any]]]


def accepted_chain(
    c: Any, catalog: WorkflowIncrementCatalog, tip: str, checkpoint: Callable[[], None]
) -> Chain:
    result: Chain = []
    seen: set[str] = set()
    current = tip
    for _ in range(MAX_CHAIN_NODES):
        checkpoint()
        require(current not in seen, "increment parent cycle")
        seen.add(current)
        record, _ = catalog.parent(c, current)
        check_anchor(c, catalog.subject, record["source"]["event_head"])
        kind = "base" if PUBLICATION.fullmatch(current) else "increment"
        result.append((kind, record))
        if kind == "base":
            result.reverse()
            return result
        current = record["parent"]["publication_id"]
    require(False, "increment chain depth exceeded")
    return []


def verify_source_prefix(
    c: Any, database: Database, parent: dict[str, Any], checkpoint: Callable[[], None]
) -> None:
    source = parent["source"]
    with (
        closing(
            cast(
                Generator[dict[str, Any], None, None],
                _history(c, database, source["subject_id"], source["workflow_id"], checkpoint),
            )
        ) as rows,
        closing(_verified_records(islice(rows, source["records"]), source, checkpoint)) as proof,
    ):
        for _ in proof:
            pass


def write_increment(
    c: Any,
    database: Database,
    store: WorkflowArtifactStore,
    record: dict[str, Any],
    checkpoint: Callable[[], None],
) -> dict[str, Any]:
    chunks: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    size = count = 0

    def flush() -> None:
        nonlocal pending, size
        if not pending:
            return
        checkpoint()
        require(len(chunks) < MAX_SEGMENTS, "increment segment budget exceeded")
        encoded = canonical_json({"format": FORMAT, "records": pending}).encode()
        require(len(encoded) <= MAX_CHUNK_BYTES, "increment chunk exceeds budget")
        chunks.append(
            {
                **store.put(f"segment-{len(chunks):06d}.enc", encoded),
                "records": len(pending),
                "first": pending[0]["row"]["checkpoint_version"],
                "last": pending[-1]["row"]["checkpoint_version"],
            }
        )
        pending, size = [], 0

    source = record["source"]
    with closing(
        cast(
            Generator[dict[str, Any], None, None],
            _history(c, database, source["subject_id"], source["workflow_id"], checkpoint),
        )
    ) as rows:
        for row in rows:
            if row["row"]["checkpoint_version"] < record["first_version"]:
                continue
            length = len(canonical_json(row).encode())
            require(length <= MAX_CHUNK_BYTES - 1024, "increment row exceeds chunk budget")
            if pending and (len(pending) >= CHUNK_ROWS or size + length > CHUNK_TARGET_BYTES):
                flush()
            pending.append(row)
            size += length
            count += 1
    flush()
    require(
        count == record["last_version"] - record["first_version"] + 1,
        "increment suffix count differs",
    )
    manifest = canonical_json(
        {
            "format": FORMAT,
            "binding": WorkflowIncrementCatalog._binding(record),
            "segments": chunks,
        }
    ).encode()
    require(len(manifest) <= MAX_MANIFEST_BYTES, "increment manifest exceeds budget")
    checkpoint()
    return store.put("manifest.enc", manifest)


def artifact_store(
    database: Database,
    record: dict[str, Any],
    ring: ArchiveKeyring,
    *,
    base: bool,
    create: bool = False,
) -> WorkflowArtifactStore:
    store = WorkflowArtifactStore if base else WorkflowIncrementStore
    return store(
        database,
        record["subject_id"],
        record["publication_id"],
        ring,
        record["encryption_key_id"],
        record["encryption_key_fingerprint"],
        create=create,
    )


def _chain_records(
    database: Database, nodes: Chain, ring: ArchiveKeyring, checkpoint: Callable[[], None]
) -> Generator[Any, None, None]:
    total = 0
    for kind, record in nodes:
        checkpoint()
        files = artifact_store(database, record, ring, base=kind == "base")
        manifest = strict_json_loads(files.get(record["manifest"], maximum=MAX_MANIFEST_BYTES))
        require(
            isinstance(manifest, dict) and isinstance(manifest.get("segments"), list),
            "increment chain manifest is invalid",
        )
        segments = manifest["segments"]
        require(0 < len(segments) <= MAX_SEGMENTS, "increment segment count exceeds budget")
        total += len(segments)
        require(total <= MAX_CHAIN_SEGMENTS, "increment cumulative segment budget exceeded")
        if kind == "base":
            with closing(
                _bundle_records(files, record["manifest"], record["source"], checkpoint)
            ) as rows:
                yield from rows
            continue
        require(
            set(manifest) == {"format", "binding", "segments"}
            and manifest["format"] == FORMAT
            and canonical_json(manifest["binding"])
            == canonical_json(WorkflowIncrementCatalog._binding(record)),
            "increment manifest frozen binding differs",
        )
        next_version = record["first_version"]
        for index, segment in enumerate(segments):
            checkpoint()
            require(
                isinstance(segment, dict) and segment.get("name") == f"segment-{index:06d}.enc",
                "increment segment order differs",
            )
            body = strict_json_loads(files.get(segment, maximum=MAX_CHUNK_BYTES))
            require(
                isinstance(body, dict)
                and set(body) == {"format", "records"}
                and body["format"] == FORMAT
                and isinstance(body["records"], list)
                and 0 < len(body["records"]) <= CHUNK_ROWS,
                "increment chunk is invalid",
            )
            require(
                type(segment.get("records")) is int
                and type(segment.get("first")) is int
                and type(segment.get("last")) is int
                and segment["records"] == len(body["records"])
                and segment["first"] == next_version
                and segment["last"] == next_version + len(body["records"]) - 1,
                "increment segment range differs",
            )
            yield from body["records"]
            next_version += len(body["records"])
        require(next_version == record["last_version"] + 1, "increment suffix incomplete")


def verify_increment_chain(
    database: Database, nodes: Chain, ring: ArchiveKeyring, checkpoint: Callable[[], None]
) -> None:
    """Internal writer proof; candidate bindings originate from protected preparation.

    No row is returned or exposed, and no candidate becomes accepted here.
    """
    require(
        0 < len(nodes) <= MAX_CHAIN_NODES and nodes[0][0] == "base",
        "increment chain scope or depth invalid",
    )
    require(nodes[0][1]["status"] == "accepted", "increment base is not accepted")
    require(
        len({record["publication_id"] for _kind, record in nodes}) == len(nodes),
        "increment proof has duplicate nodes",
    )
    for index, (kind, record) in enumerate(nodes):
        checkpoint()
        require(
            (kind == "base" and index == 0)
            or (kind == "increment" and record["chain_depth"] == index),
            "increment chain order differs",
        )
        if index:
            require(
                type(record["first_version"]) is int
                and type(record["last_version"]) is int
                and type(record["chain_depth"]) is int
                and record["first_version"] == nodes[index - 1][1]["source"]["records"] + 1
                and record["last_version"] == record["source"]["records"],
                "increment proof range differs",
            )
            previous_kind, previous = nodes[index - 1]
            require(previous["status"] == "accepted", "increment ancestor is not accepted")
            binding = (
                WorkflowExportCatalog._binding(previous)
                if previous_kind == "base"
                else WorkflowIncrementCatalog._binding(previous)
            )
            expected_parent = {
                "kind": previous_kind,
                "publication_id": previous["publication_id"],
                "binding_hash": content_hash(binding),
                "source_json_hash": content_hash(previous["source"]),
                "manifest_hash": content_hash(previous["manifest"]),
                "acceptance_event_id": previous["receipt_event_id"],
                "acceptance_payload_hash": content_hash(
                    {
                        **binding,
                        "manifest": previous["manifest"],
                        "scope": "workflow_history_and_clock_receipts_only",
                    }
                ),
                "end_version": previous["source"]["records"],
                "chain_depth": 0 if previous_kind == "base" else previous["chain_depth"],
            }
            require(
                canonical_json(record["parent"]) == canonical_json(expected_parent),
                "increment exact parent acceptance differs",
            )
            compatible(previous["source"], record["source"])
            require(
                record["parent"]["publication_id"] == nodes[index - 1][1]["publication_id"],
                "increment chain parent differs",
            )
    expected = nodes[-1][1]["source"]
    with closing(sqlite3.connect(":memory:")) as memory:
        memory.execute("PRAGMA page_size=4096")
        memory.execute(f"PRAGMA max_page_count={MAX_SQLITE_PAGES}")
        memory.execute("PRAGMA temp_store=MEMORY")
        memory.execute("PRAGMA journal_mode=MEMORY")
        memory.execute("CREATE TABLE proof(version INTEGER PRIMARY KEY,record_json TEXT NOT NULL)")

        def stored() -> Iterator[Any]:
            for (encoded,) in memory.execute("SELECT record_json FROM proof ORDER BY version"):
                yield strict_json_loads(encoded)

        total = 0
        with (
            closing(_chain_records(database, nodes, ring, checkpoint)) as rows,
            closing(_verified_records(rows, expected, checkpoint)) as verified,
        ):
            for row in verified:
                encoded = canonical_json(row)
                total += len(encoded.encode())
                require(total <= MAX_RECONSTRUCTION_BYTES, "increment chain proof exceeds budget")
                memory.execute(
                    "INSERT INTO proof VALUES(?,?)", (row["row"]["checkpoint_version"], encoded)
                )
        memory.commit()
        memory.execute("PRAGMA query_only=ON")
        # Re-read the records actually retained, proving every accepted boundary.
        # The depth cap bounds this conservative O(depth * history) proof.
        for _kind, record in nodes:
            with closing(
                _verified_records(
                    islice(stored(), record["source"]["records"]), record["source"], checkpoint
                )
            ) as proof:
                for _ in proof:
                    pass
        checkpoint()
