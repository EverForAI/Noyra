"""Read-only, bounded usage windows with independent call/attempt clocks."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .database import Database


class UsageQueryLimit(ValueError):
    pass


def usage_window(
    window: str, offset_minutes: int, now: datetime
) -> tuple[datetime | None, datetime]:
    if window not in {"today", "3", "7", "30", "90", "all"}:
        raise ValueError("invalid usage window")
    if not -840 <= offset_minutes <= 840:
        raise ValueError("invalid timezone offset")
    if window == "all":
        return None, now
    if window == "today":
        local = now.astimezone(timezone(timedelta(minutes=offset_minutes)))
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC), now
    return now - timedelta(days=int(window)), now


class UsageProjection:
    def __init__(self, database: Database) -> None:
        self.database = database

    def summary(
        self, subject_id: str, *, window: str = "7", offset_minutes: int = 0,
        pool: str | None = None, resource: str | None = None, now: datetime | None = None,
    ) -> dict[str, Any]:
        observed = now or datetime.now(UTC)
        start, end = usage_window(window, offset_minutes, observed)
        if pool not in {None, "economy", "deep"} or (resource and len(resource) > 128):
            raise ValueError("invalid resource filter")
        counters = dict.fromkeys((
            "calls", "succeeded_calls", "failed_calls", "unknown_calls", "attempts",
            "succeeded_attempts", "failed_attempts", "unknown_attempts", "retry_attempts",
            "input_tokens", "output_tokens", "cost_microusd", "known_cost_microusd",
            "estimated_cost_microusd", "estimated_cost_attempts", "unknown_price_attempts",
            "pending_untimed_attempts",
            "untimed_terminal_attempts",
        ), 0)
        deadline = time.monotonic() + 5
        params: list[Any] = [subject_id, (start or datetime.min.replace(tzinfo=UTC)).isoformat(),
                             end.isoformat()]
        scope = ""
        if pool:
            scope += " AND c.resource_pool=?"
            params.append(pool)
        if resource:
            scope += " AND c.resource_group_id=?"
            params.append(resource)
        search: dict[str, Any] = {
            "runs": 0, "by_method": {method: {} for method in ("api", "model", "browser", "wait")}
        }
        with self.database.read_transaction() as connection:
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 2_000)

            def bounded(sql: str, arguments: list[Any]) -> Any:
                for index, row in enumerate(connection.execute(sql, arguments)):
                    if index >= 200_000 or time.monotonic() > deadline:
                        raise UsageQueryLimit("usage query exceeds read budget")
                    yield row

            for row in bounded(
                "SELECT c.status FROM model_calls c WHERE c.subject_id=? "
                "AND c.created_at>=? AND c.created_at<=?" + scope, params,
            ):
                counters["calls"] += 1
                if row["status"] in {"succeeded", "failed", "unknown"}:
                    counters[row["status"] + "_calls"] += 1
            for row in bounded(
                "SELECT a.*, c.usage_estimated, g.input_microusd_per_million input_price, "
                "g.output_microusd_per_million output_price, p.pricing_status, "
                "p.updated_at pricing_updated_at FROM model_attempts a "
                "JOIN model_calls c ON c.call_id=a.call_id AND c.subject_id=a.subject_id "
                "LEFT JOIN cognitive_resource_groups g ON g.group_id=c.resource_group_id "
                "LEFT JOIN cognitive_resource_limit_policies p ON p.group_id=g.group_id "
                "WHERE c.subject_id=? AND a.started_at>=? AND a.started_at<=?" + scope, params,
            ):
                counters["attempts"] += 1
                if row["status"] in {"succeeded", "failed", "unknown"}:
                    counters[row["status"] + "_attempts"] += 1
                counters["retry_attempts"] += int(row["attempt_number"] > 1)
                for field in ("input_tokens", "output_tokens"):
                    counters[field] += int(row[field] or 0)
                cost = int(row["cost_microusd"] or 0)
                estimated = row["cost_microusd"] is None or row["usage_estimated"] == 1
                if row["cost_microusd"] is None:
                    cost = int(row["reserved_cost_microusd"])
                counters["cost_microusd"] += cost
                counters["estimated_cost_microusd" if estimated else "known_cost_microusd"] += cost
                counters["estimated_cost_attempts"] += int(estimated)
                confirmed_free = (
                    row["pricing_status"] == "free"
                    and row["pricing_updated_at"] <= row["started_at"]
                )
                counters["unknown_price_attempts"] += int(
                    cost == 0 and not confirmed_free and (
                        not row["pricing_updated_at"]
                        or row["pricing_updated_at"] > row["started_at"]
                        or (not row["input_price"] and not row["output_price"])
                    )
                )
            counters["pending_untimed_attempts"] = int(connection.execute(
                "SELECT COUNT(*) FROM model_attempts a JOIN model_calls c ON c.call_id=a.call_id "
                "WHERE c.subject_id=? AND a.started_at IS NULL "
                "AND a.status IN ('authorized','executing','unknown')" + scope,
                [subject_id, *params[3:]],
            ).fetchone()[0])
            counters["untimed_terminal_attempts"] = int(connection.execute(
                "SELECT COUNT(*) FROM model_attempts a JOIN model_calls c ON c.call_id=a.call_id "
                "WHERE c.subject_id=? AND a.started_at IS NULL "
                "AND a.status IN ('failed','succeeded','cancelled')" + scope,
                [subject_id, *params[3:]],
            ).fetchone()[0])
            search_params = params[:3]
            for row in bounded(
                "SELECT final_method, status FROM research_search_runs "
                "WHERE subject_id=? AND created_at>=? AND created_at<=?", search_params,
            ):
                statuses = search["by_method"].setdefault(row["final_method"], {})
                statuses[row["status"]] = statuses.get(row["status"], 0) + 1
                search["runs"] += 1
            earliest = connection.execute(
                "SELECT MIN(created_at) FROM model_calls WHERE subject_id=?", (subject_id,),
            ).fetchone()[0]
            earliest_search = connection.execute(
                "SELECT MIN(created_at) FROM research_search_runs WHERE subject_id=?",
                (subject_id,),
            ).fetchone()[0]
        return {
            **{name: str(value) for name, value in counters.items()},
            "window": window, "window_days": None if window in {"today", "all"} else int(window),
            "since": None if start is None else start.isoformat(), "until": end.isoformat(),
            "sampled_at": observed.isoformat(), "timezone_offset_minutes": offset_minutes,
            "earliest_model_record_at": earliest, "earliest_search_record_at": earliest_search,
            "coverage": "all_retained_records", "budget_timezone": "UTC", "search": search,
            "attempt_clock": "started_at", "search_scope": "all_resources",
        }
