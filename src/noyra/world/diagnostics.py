"""Bounded, operator-only projection; no fetches, model calls or archive reads."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from noyra.core.database import Database
from noyra.core.types import utc_now

from .injection import INJECTION_CLASSIFIER_VERSION
from .poll import MAX_SEEN_VERSIONS, SourcePollStore

SOURCE_LIMIT = 25
QUERY_BUDGET_SECONDS = 0.2


class AcquisitionQueryLimit(RuntimeError):
    pass


class WorldAcquisitionProjection:
    def __init__(self, database: Database):
        self.database = database

    def summary(self, subject_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + QUERY_BUDGET_SECONDS
        items: list[dict[str, Any]] = []
        with self.database.connection() as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=100")
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1_000)
            try:
                connection.execute("BEGIN")
                sources = connection.execute(
                    "SELECT source_id,name,source_type,status FROM world_sources "
                    "WHERE subject_id=? ORDER BY (status='active') DESC,source_id LIMIT ?",
                    (subject_id, SOURCE_LIMIT + 1),
                ).fetchall()
                for source in sources[:SOURCE_LIMIT]:
                    if time.monotonic() >= deadline:
                        raise AcquisitionQueryLimit("world acquisition query exceeded its budget")
                    source_id = str(source["source_id"])
                    state = SourcePollStore._read(connection, subject_id, source_id)
                    seen = connection.execute(
                        "SELECT COUNT(*) FROM world_feed_seen WHERE subject_id=? AND source_id=?",
                        (subject_id, source_id),
                    ).fetchone()[0]
                    items.append(
                        {
                            "source_id": source_id,
                            "name": str(source["name"]),
                            "source_type": str(source["source_type"]),
                            "source_status": str(source["status"]),
                            "tracking": "uninitialized" if state is None else "available",
                            "last_attempt_at": state["last_attempt_at"] if state else None,
                            "last_success_at": state["last_success_at"] if state else None,
                            "last_change_at": state["last_change_at"] if state else None,
                            "outcome": state["outcome"] if state else None,
                            "checks": int(state["checks"]) if state else None,
                            "unchanged_checks": int(state["unchanged_checks"]) if state else None,
                            "new_item_versions": int(state["new_item_versions"]) if state else None,
                            "fingerprint_version": state["fingerprint_version"] if state else None,
                            "baseline": state["baseline"] if state else None,
                            "retained_entry_versions": int(seen),
                        }
                    )
                if time.monotonic() >= deadline:
                    raise AcquisitionQueryLimit("world acquisition query exceeded its budget")
            except sqlite3.OperationalError as error:
                raise AcquisitionQueryLimit(
                    "world acquisition state temporarily unavailable"
                ) from error
            finally:
                connection.set_progress_handler(None, 0)
                connection.rollback()
        return {
            "format": "noyra-world-acquisition/v1",
            "sampled_at": utc_now(),
            "scope": "since_tracking_started",
            "source_limit": SOURCE_LIMIT,
            "truncated": len(sources) > SOURCE_LIMIT,
            "items": items,
            "current_classifier_version": INJECTION_CLASSIFIER_VERSION,
            "classifier_version_scope": "current_rules_not_historical_reclassification",
            "retained_version_limit_per_source": MAX_SEEN_VERSIONS,
        }
