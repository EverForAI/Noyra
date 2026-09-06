from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from noyra.core import EventStore, SubjectKernel
from noyra.core.types import canonical_json, content_hash, new_id
from noyra.interaction import PublicProjection


def make_projection(tmp_path: Path) -> tuple[SubjectKernel, PublicProjection]:
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        "Noyra-runtime-log-keyset",
        content_hash({"seed": "runtime-log-keyset"}),
    )
    return kernel, PublicProjection(kernel.database)


def insert_records(kernel: SubjectKernel, start: int, count: int, *, prefix: str) -> None:
    events = EventStore(kernel.database)
    for index in range(start, start + count):
        occurred_at = f"2026-08-17T00:{index // 60:02d}:{index % 60:02d}.000+00:00"
        events.append(
            kernel.subject_id,
            f"{prefix}-event",
            "keyset-test",
            {"index": index},
            occurred_at=occurred_at,
        )
        with kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_records(audit_id, subject_id, action, actor, payload_json, "
                "occurred_at) VALUES (?, ?, ?, 'keyset-test', ?, ?)",
                (
                    new_id(f"{prefix}-audit"),
                    kernel.subject_id,
                    f"{prefix}-action",
                    canonical_json({"index": index}),
                    occurred_at,
                ),
            )


def test_runtime_log_queries_use_explicit_keyset_indexes_without_offset() -> None:
    with tempfile.TemporaryDirectory() as directory:
        kernel, projection = make_projection(Path(directory))
        try:
            with kernel.database.connection() as connection:
                for source in projection.RUNTIME_LOG_SOURCES:
                    query, parameters = projection._runtime_log_query(
                        source,
                        kernel.subject_id,
                        1_000_000,
                        101,
                        ("2026-08-17T00:00:00.000+00:00", "model_call", "record"),
                    )
                    assert "OFFSET" not in query.upper()
                    plan = " ".join(
                        str(row["detail"])
                        for row in connection.execute(
                            "EXPLAIN QUERY PLAN " + query, parameters
                        ).fetchall()
                    )
                    assert source[6] in plan
                    assert "USE TEMP B-TREE" not in plan
        finally:
            kernel.close()


def test_cursor_high_water_prevents_concurrent_duplicates_and_gaps() -> None:
    with tempfile.TemporaryDirectory() as directory:
        kernel, projection = make_projection(Path(directory))
        try:
            insert_records(kernel, 0, 30, prefix="initial")
            baseline_page = projection.runtime_logs(kernel.subject_id, limit=1_000)
            baseline = baseline_page["items"]
            assert baseline_page["next_cursor"] is None

            first = projection.runtime_logs(kernel.subject_id, limit=7)
            assert first["next_cursor"] is not None
            insert_records(kernel, 30, 5, prefix="concurrent-newer")
            insert_records(kernel, 5, 5, prefix="concurrent-backdated")

            replay_one = projection.runtime_logs(
                kernel.subject_id, limit=7, cursor=str(first["next_cursor"])
            )
            replay_two = projection.runtime_logs(
                kernel.subject_id, limit=7, cursor=str(first["next_cursor"])
            )
            assert replay_one == replay_two

            items = list(first["items"])
            page = replay_one
            while True:
                items.extend(page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
                page = projection.runtime_logs(kernel.subject_id, limit=7, cursor=str(cursor))

            assert items == baseline
            keys = [(item["occurred_at"], item["category"], item["record_id"]) for item in items]
            assert len(keys) == len(set(keys))
        finally:
            kernel.close()


def test_large_runtime_log_profile_keeps_anchor_and_page_work_bounded() -> None:
    with tempfile.TemporaryDirectory() as directory:
        kernel, projection = make_projection(Path(directory))
        try:
            row_count = 25_000
            payload_json = canonical_json({"profile": True})
            payload_hash = content_hash({"profile": True})
            event_rows = []
            audit_rows = []
            for index in range(row_count):
                occurred_at = (
                    f"2026-08-17T{index // 3_600:02d}:"
                    f"{(index // 60) % 60:02d}:{index % 60:02d}.000+00:00"
                )
                event_rows.append(
                    (
                        f"profile-event-{index:08d}",
                        kernel.subject_id,
                        "profile-event",
                        "keyset-test",
                        occurred_at,
                        occurred_at,
                        payload_json,
                        payload_hash,
                        "private",
                        "[]",
                        "recorded",
                    )
                )
                audit_rows.append(
                    (
                        f"profile-audit-{index:08d}",
                        kernel.subject_id,
                        "profile-action",
                        "keyset-test",
                        payload_json,
                        occurred_at,
                    )
                )
            with kernel.database.transaction() as connection:
                connection.executemany(
                    "INSERT INTO events(event_id, subject_id, event_type, source, "
                    "occurred_at, observed_at, payload_json, payload_hash, privacy_level, "
                    "causal_parent_ids_json, processing_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    event_rows,
                )
                connection.executemany(
                    "INSERT INTO audit_records(audit_id, subject_id, action, actor, "
                    "payload_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?)",
                    audit_rows,
                )

            original_connect = kernel.database._connect
            progress_callbacks = 0

            def profiled_connect() -> sqlite3.Connection:
                nonlocal progress_callbacks
                connection = original_connect()

                def progress() -> int:
                    nonlocal progress_callbacks
                    progress_callbacks += 1
                    return 0

                connection.set_progress_handler(progress, 100)
                return connection

            kernel.database._connect = profiled_connect  # type: ignore[method-assign]
            first = projection.runtime_logs(kernel.subject_id, limit=100)
            first_page_callbacks = progress_callbacks
            progress_callbacks = 0
            second = projection.runtime_logs(
                kernel.subject_id,
                limit=100,
                cursor=str(first["next_cursor"]),
            )

            assert len(first["items"]) == 100
            assert len(second["items"]) == 100
            assert first["next_cursor"] is not None
            assert second["next_cursor"] is not None
            assert first_page_callbacks < 250
            assert progress_callbacks < 250
        finally:
            kernel.close()
