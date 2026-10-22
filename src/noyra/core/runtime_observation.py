"""Bounded, read-only operational samples; never used for admission or billing."""

from __future__ import annotations

import copy
import os
import shutil
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .cognitive_waits import terminal_wait_reason

CALL_LIMIT = 100
ATTEMPT_LIMIT = 500
QUERY_SECONDS = 0.25
CACHE_SECONDS = 2.0


class ObservationBusy(RuntimeError):
    pass


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def loop_observation(
    row: dict[str, Any] | None, lifecycle: str | None, *, now: datetime, stale_after: float
) -> dict[str, Any]:
    last = _timestamp(None if row is None else row.get("last_successful_tick_at"))
    retry = _timestamp(None if row is None else row.get("next_retry_at"))
    age = None if last is None else (now - last).total_seconds()
    circuit = None if row is None else row.get("circuit_status")
    if lifecycle == "paused":
        reason = "paused"
    elif lifecycle in {"winding_down", "reflective_sleep", "deep_sleep", "waking"}:
        reason = "resting"
    elif lifecycle == "stopped":
        reason = "stopped"
    elif circuit in {"open", "half_open"}:
        reason = "cooling_down" if circuit == "open" else "recovering"
    elif row is None or age is None or age < 0 or lifecycle != "active" or circuit != "closed":
        reason = "insufficient_evidence"
    elif age > stale_after:
        reason = "tick_not_recent"
    else:
        reason = "recent_success"
    return {
        "reason_code": reason,
        "circuit_status": circuit if circuit in {"closed", "open", "half_open"} else None,
        "last_successful_tick_at": None if last is None else last.isoformat(),
        "last_success_age_seconds": None if age is None or age < 0 else round(age, 3),
        "next_retry_at": None if retry is None else retry.isoformat(),
        "consecutive_failures": None if row is None else int(row["consecutive_failures"]),
        "observation_threshold_seconds": stale_after,
    }


def resource_observation(data: Path, database: Path) -> dict[str, Any]:
    # No directory walks, integrity scans, subprocesses, sockets, or secret reads.
    result: dict[str, Any] = {
        "rss_bytes": None,
        "data_free_bytes": None,
        "data_total_bytes": None,
        "database_bytes": None,
        "wal_bytes": None,
    }
    if os.name == "posix":
        try:
            with Path("/proc/self/statm").open(encoding="ascii") as stream:
                fields = stream.read(1024).split()
            sysconf = getattr(os, "sysconf", None)
            if sysconf is not None:
                result["rss_bytes"] = int(fields[1]) * sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            pass
    try:
        usage = shutil.disk_usage(data)
        result.update(data_free_bytes=usage.free, data_total_bytes=usage.total)
    except OSError:
        pass
    for key, path in (
        ("database_bytes", database),
        ("wal_bytes", database.with_name(database.name + "-wal")),
    ):
        try:
            result[key] = path.stat().st_size
        except FileNotFoundError:
            # Missing DB is unknown, not a healthy zero-byte database.
            result[key] = 0 if key == "wal_bytes" else None
        except OSError:
            pass
    return result


class RuntimeObserver:
    """Coalesce display samples, with bounded lock wait, rows, SQL work and TTL."""

    def __init__(self, database: Path, data: Path, subject_id: str) -> None:
        self.database = database
        self.data = data
        self.subject_id = subject_id
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    def snapshot(self, *, cognition_enabled: bool, interval_seconds: float) -> dict[str, Any]:
        if not self._lock.acquire(timeout=0.05):
            raise ObservationBusy("runtime_observation_busy")
        try:
            if self._cached is None or time.monotonic() - self._cached_at >= CACHE_SECONDS:
                self._cached = self._collect(cognition_enabled, interval_seconds)
                self._cached_at = time.monotonic()
            result = copy.deepcopy(self._cached)
            result["cache_age_seconds"] = round(time.monotonic() - self._cached_at, 3)
            return result
        finally:
            self._lock.release()

    def _collect(self, cognition_enabled: bool, interval_seconds: float) -> dict[str, Any]:
        started = time.monotonic()
        now = datetime.now(UTC)
        result: dict[str, Any] = {
            "format": "noyra-runtime-observation/v1",
            "observed_at": now.isoformat(),
            "scope": "local_observation_not_readiness_or_provider_probe",
            "cognition": {"enabled": cognition_enabled},
            "database_observation": "unavailable",
            "lifecycle": None,
            "loop": None,
            "model_sample": None,
            "waiting_sample": None,
            "attention": None,
            "resources": resource_observation(self.data, self.database),
        }
        connection = None
        try:
            connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=0.1)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + QUERY_SECONDS
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
            connection.execute("BEGIN")
            state = connection.execute(
                "SELECT state FROM runtime_state WHERE subject_id=?", (self.subject_id,)
            ).fetchone()
            row = connection.execute(
                "SELECT circuit_status,consecutive_failures,last_successful_tick_at,next_retry_at "
                "FROM autonomy_loop_state WHERE subject_id=?",
                (self.subject_id,),
            ).fetchone()
            calls = connection.execute(
                "SELECT call_id,status,usage_estimated,created_at,completed_at,resource_group_id,"
                "resource_pool,model,purpose,error_code FROM model_calls "
                "WHERE subject_id=? ORDER BY created_at DESC,call_id DESC LIMIT ?",
                (self.subject_id, CALL_LIMIT + 1),
            ).fetchall()
            call_ids = [str(call["call_id"]) for call in calls[:CALL_LIMIT]]
            attempts = []
            if call_ids:
                marks = ",".join("?" for _ in call_ids)
                attempts = connection.execute(
                    "SELECT status,input_tokens,output_tokens,cost_microusd FROM model_attempts "
                    f"WHERE call_id IN ({marks}) AND subject_id=? ORDER BY call_id,attempt_number "
                    "LIMIT ?",
                    (*call_ids, self.subject_id, ATTEMPT_LIMIT + 1),
                ).fetchall()
            attention: dict[str, Any] = {}
            for name, table in (
                ("unknown_actions", "actions"),
                ("unknown_model_calls", "model_calls"),
                ("unknown_payments", "wallet_payment_executions"),
            ):
                # Fixed table names, indexed subject/status lookup, bounded counts.
                found = connection.execute(
                    f"SELECT 1 FROM {table} WHERE subject_id=? AND status='unknown' LIMIT 51",
                    (self.subject_id,),
                ).fetchall()
                attention[name] = {"count": min(50, len(found)), "more": len(found) > 50}
            deliveries = connection.execute(
                "SELECT 1 FROM interaction_deliveries d WHERE d.subject_id=? "
                "AND d.status IN ('unknown','failed') AND NOT EXISTS "
                "(SELECT 1 FROM interaction_delivery_reconciliations r "
                "WHERE r.delivery_id=d.delivery_id "
                "AND r.outcome IN ('delivered','failed','cancelled')) "
                "LIMIT 51",
                (self.subject_id,),
            ).fetchall()
            attention["unresolved_deliveries"] = {
                "count": min(50, len(deliveries)),
                "more": len(deliveries) > 50,
            }
            waits = connection.execute(
                "SELECT task_id,purpose,status FROM waiting_cognitive_tasks "
                "WHERE subject_id=? AND status IN ('waiting','cancelled') "
                "ORDER BY rowid DESC LIMIT ?",
                (self.subject_id, CALL_LIMIT + 1),
            ).fetchall()
            waiting_sample: dict[str, Any] = {
                "scope": "latest_wait_records_not_total",
                "record_limit": CALL_LIMIT,
                "records": min(len(waits), CALL_LIMIT),
                "truncated": len(waits) > CALL_LIMIT,
                "active": 0,
                "terminal_parent": 0,
                "cancelled": 0,
                "missing_parent": 0,
            }
            for wait in waits[:CALL_LIMIT]:
                if wait["status"] == "cancelled":
                    waiting_sample["cancelled"] += 1
                elif terminal_wait_reason(connection, self.subject_id, wait["purpose"]) is not None:
                    waiting_sample["terminal_parent"] += 1
                else:
                    waiting_sample["active"] += 1
                    kind, _, parent_id = wait["purpose"].partition(":")
                    parent = None
                    if kind == "sleep_reflection":
                        parent = connection.execute(
                            "SELECT 1 FROM sleep_runs WHERE subject_id=? AND sleep_id=?",
                            (self.subject_id, parent_id),
                        ).fetchone()
                    elif kind == "world_cognition":
                        parent = connection.execute(
                            "SELECT 1 FROM observations WHERE subject_id=? AND observation_id=?",
                            (self.subject_id, parent_id),
                        ).fetchone()
                    if kind in {"sleep_reflection", "world_cognition"} and parent is None:
                        waiting_sample["missing_parent"] += 1
            selected = calls[:CALL_LIMIT]
            by_resource: dict[tuple[str | None, str, str], dict[str, Any]] = {}
            unknown_reasons: dict[str, int] = {}
            for call in selected:
                key = (call["resource_group_id"], call["resource_pool"], call["model"])
                group = by_resource.setdefault(
                    key,
                    {
                        "resource_id": key[0],
                        "pool": key[1],
                        "model": key[2],
                        "calls": 0,
                        "succeeded": 0,
                        "failed": 0,
                        "unknown": 0,
                        "pending": 0,
                        "operator_tests": 0,
                    },
                )
                group["calls"] += 1
                if call["status"] in {"succeeded", "failed", "unknown"}:
                    group[call["status"]] += 1
                elif call["status"] in {"prepared", "executing"}:
                    group["pending"] += 1
                if call["purpose"].startswith("operator_model_test:"):
                    group["operator_tests"] += 1
                if call["status"] == "unknown":
                    code = call["error_code"]
                    if code not in {
                        "provider_outcome_unknown",
                        "provider_http_502",
                        "provider_http_503",
                        "provider_http_504",
                        "provider_timeout",
                        "provider_transport_timeout",
                    }:
                        code = "other_or_unclassified"
                    unknown_reasons[code] = unknown_reasons.get(code, 0) + 1

            bounded_attempts = attempts[:ATTEMPT_LIMIT]
            times = [_timestamp(call["created_at"]) for call in selected]
            timestamps = [value for value in times if value is not None]
            durations = []
            for call in selected:
                begin = _timestamp(call["created_at"])
                finish = _timestamp(call["completed_at"])
                if begin is not None and finish is not None and finish >= begin:
                    durations.append(round((finish - begin).total_seconds() * 1000, 3))
            durations.sort()
            lifecycle = None if state is None else str(state["state"])
            result.update(
                {
                    "database_observation": "ok",
                    "attention": attention,
                    "lifecycle": lifecycle,
                    "loop": loop_observation(
                        None if row is None else dict(row),
                        lifecycle,
                        now=now,
                        stale_after=max(180.0, interval_seconds * 3),
                    ),
                    "waiting_sample": waiting_sample,
                    "model_sample": {
                        "by_resource": list(by_resource.values()),
                        "unknown_call_reasons": unknown_reasons,
                        "pending_calls": sum(
                            call["status"] in {"prepared", "executing"} for call in selected
                        ),
                        "operator_test_calls": sum(
                            call["purpose"].startswith("operator_model_test:") for call in selected
                        ),
                        "scope": "latest_calls_not_daily_total",
                        "call_limit": CALL_LIMIT,
                        "attempt_limit": ATTEMPT_LIMIT,
                        "calls": len(selected),
                        "calls_truncated": len(calls) > CALL_LIMIT,
                        "attempts": len(bounded_attempts),
                        "attempts_truncated": len(attempts) > ATTEMPT_LIMIT,
                        "call_elapsed_samples": len(durations),
                        "call_elapsed_ms_p50": durations[(len(durations) - 1) // 2]
                        if durations
                        else None,
                        "call_elapsed_ms_p95": durations[
                            max(0, (95 * len(durations) + 99) // 100 - 1)
                        ]
                        if durations
                        else None,
                        "succeeded_calls": sum(call["status"] == "succeeded" for call in selected),
                        "failed_calls": sum(call["status"] == "failed" for call in selected),
                        "unknown_calls": sum(call["status"] == "unknown" for call in selected),
                        "estimated_calls": sum(bool(call["usage_estimated"]) for call in selected),
                        "known_cost_microusd": str(
                            sum(int(a["cost_microusd"] or 0) for a in bounded_attempts)
                        ),
                        "missing_cost_attempts": sum(
                            a["cost_microusd"] is None for a in bounded_attempts
                        ),
                        "input_tokens": sum(int(a["input_tokens"] or 0) for a in bounded_attempts),
                        "output_tokens": sum(
                            int(a["output_tokens"] or 0) for a in bounded_attempts
                        ),
                        "since": min(timestamps).isoformat() if timestamps else None,
                        "until": max(timestamps).isoformat() if timestamps else None,
                    },
                }
            )
        except (OSError, sqlite3.Error):
            # Incomplete queries never masquerade as zero usage or healthy state.
            pass
        finally:
            if connection is not None:
                connection.close()
        result["collection_ms"] = round((time.monotonic() - started) * 1000, 3)
        return result
