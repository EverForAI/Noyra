"""Validation HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import re
from typing import Any, cast

from noyra.core.database import Database
from noyra.model import CognitiveResourceGroupRecord
from noyra.model.capabilities import CapabilityProfileStore
from noyra.model.reasoning import reasoning_profile_payload
from noyra.wallet import (
    ACQUISITION_RUN_STATES,
    WALLET_BALANCE_OBSERVATION_MAX_GROUPS,
    WALLET_BALANCE_OBSERVATION_MAX_WINDOW_SECONDS,
)
from noyra.wallet.types import canonical_timestamp

_WALLET_HEALTH_STATUSES = frozenset({"ok", "attention", "degraded"})


_WALLET_OBSERVATION_SOURCE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


_WALLET_OBSERVATION_FIELDS = frozenset(
    {
        "status",
        "evaluated_at",
        "stale_after_seconds",
        "observed_pairs",
        "fresh_pairs",
        "near_expiry_pairs",
        "stale_pairs",
        "never_pairs",
        "future_pairs",
        "anomalous_pairs",
        "max_age_seconds",
    }
)


_WALLET_BREAKDOWN_FIELDS = frozenset(
    {
        "group_by",
        "evaluated_at",
        "stale_after_seconds",
        "status",
        "groups",
        "total_groups",
        "has_more",
    }
)


_WALLET_BREAKDOWN_GROUP_FIELDS = frozenset(
    {
        "value",
        "active_pairs",
        "observed_pairs",
        "fresh_pairs",
        "near_expiry_pairs",
        "stale_pairs",
        "never_pairs",
        "future_pairs",
        "anomalous_pairs",
        "snapshot_count",
        "latest_observed_at",
        "max_age_seconds",
        "status",
    }
)


_WALLET_BREAKDOWN_COUNT_FIELDS = (
    "active_pairs",
    "observed_pairs",
    "fresh_pairs",
    "near_expiry_pairs",
    "stale_pairs",
    "never_pairs",
    "future_pairs",
    "anomalous_pairs",
    "snapshot_count",
)


_WALLET_OBSERVATION_COUNT_FIELDS = (
    "observed_pairs",
    "fresh_pairs",
    "near_expiry_pairs",
    "stale_pairs",
    "never_pairs",
    "future_pairs",
    "anomalous_pairs",
)


_WALLET_RESOURCE_FIELDS = frozenset({"active", "revoked", "total"})


_WALLET_RESOURCE_NAMES = ("networks", "assets", "addresses")


_WALLET_BALANCE_HISTORY_FIELDS = frozenset(
    {"snapshots", "latest_observed_at", "latest_created_at", "observation_health"}
)


_WALLET_SUMMARY_FIELDS = frozenset({"subject_id", "resources", "balance_history"})


_WALLET_ACQUISITION_FIELDS = frozenset(
    {
        "subject_id",
        "counts",
        "total",
        "active",
        "attention",
        "attempts",
        "expired_running",
        "next_attempt_at",
    }
)


_WALLET_ACQUISITION_FIELDS_WITH_STATUS = _WALLET_ACQUISITION_FIELDS | {"status"}


_WALLET_ACQUISITION_COUNT_FIELDS = frozenset(ACQUISITION_RUN_STATES)


_WALLET_MAX_INT = 9_223_372_036_854_775_807


_MODEL_RESOURCE_EXACT_FIELDS = (
    "priority",
    "weight",
    "daily_attempts",
    "daily_input_tokens",
    "daily_output_tokens",
    "daily_cost_microusd",
    "input_microusd_per_million",
    "output_microusd_per_million",
    "max_attempts",
)


def _cognitive_resource_payload(
    record: CognitiveResourceGroupRecord, database: Database | None = None
) -> dict[str, Any]:
    payload = dict(record.__dict__)
    payload["exact_budget"] = {
        field: str(getattr(record, field)) for field in _MODEL_RESOURCE_EXACT_FIELDS
    }
    capability = (
        CapabilityProfileStore(database).get(
            record.subject_id, record.provider_type, record.base_url, record.model
        )
        if database is not None
        else None
    )
    payload["reasoning_profile"] = (
        capability.public(selected=record.reasoning_effort)
        if capability is not None
        else reasoning_profile_payload(
            record.model, record.provider_type, selected=record.reasoning_effort
        )
    )
    return payload


def _wallet_unavailable() -> dict[str, str]:
    """Return the only degraded payload that may cross the wallet boundary."""

    return {"status": "degraded", "reason": "unavailable"}


def _wallet_exact_dict(value: object, fields: frozenset[str], label: str) -> dict[str, Any]:
    """Validate an aggregate mapping without retaining unapproved fields."""

    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} projection is invalid")
    return cast(dict[str, Any], value)


def _wallet_nonnegative_int(value: object, label: str) -> int:
    """Validate bounded integer counters (``bool`` is deliberately rejected)."""

    if type(value) is not int or not 0 <= value <= _WALLET_MAX_INT:
        raise ValueError(f"{label} is invalid")
    return value


def _wallet_timestamp(value: object, label: str, *, nullable: bool = False) -> str | None:
    """Accept only canonical, timezone-aware UTC-millisecond timestamps."""

    if value is None and nullable:
        return None
    if type(value) is not str or not value or len(value) > 64:
        raise ValueError(f"{label} is invalid")
    try:
        canonical = canonical_timestamp(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} is invalid") from error
    if canonical != value:
        raise ValueError(f"{label} is not canonical")
    return value


def _wallet_resource_counts(value: object, label: str) -> dict[str, int]:
    payload = _wallet_exact_dict(value, _WALLET_RESOURCE_FIELDS, label)
    active = _wallet_nonnegative_int(payload["active"], f"{label}.active")
    revoked = _wallet_nonnegative_int(payload["revoked"], f"{label}.revoked")
    total = _wallet_nonnegative_int(payload["total"], f"{label}.total")
    if total != active + revoked:
        raise ValueError(f"{label}.total is inconsistent")
    return {"active": active, "revoked": revoked, "total": total}


def _wallet_observation_health(value: object) -> dict[str, Any]:
    """Project and validate the complete Stage 4-A-7 observation contract."""

    payload = _wallet_exact_dict(value, _WALLET_OBSERVATION_FIELDS, "observation health")
    status = payload["status"]
    if type(status) is not str or status not in _WALLET_HEALTH_STATUSES:
        raise ValueError("observation health status is invalid")
    evaluated_at = _wallet_timestamp(payload["evaluated_at"], "observation evaluated_at")
    stale_after_seconds = _wallet_nonnegative_int(
        payload["stale_after_seconds"], "observation stale_after_seconds"
    )
    if stale_after_seconds > WALLET_BALANCE_OBSERVATION_MAX_WINDOW_SECONDS:
        raise ValueError("observation stale_after_seconds exceeds the configured bound")

    counters = {
        field: _wallet_nonnegative_int(payload[field], f"observation {field}")
        for field in _WALLET_OBSERVATION_COUNT_FIELDS
    }
    if (
        counters["fresh_pairs"]
        + counters["near_expiry_pairs"]
        + counters["stale_pairs"]
        + counters["future_pairs"]
        != counters["observed_pairs"]
    ):
        raise ValueError("observation pair counters are inconsistent")
    if counters["anomalous_pairs"] > counters["observed_pairs"]:
        raise ValueError("observation anomaly count is inconsistent")
    max_age_seconds = payload["max_age_seconds"]
    if max_age_seconds is not None:
        max_age_seconds = _wallet_nonnegative_int(max_age_seconds, "observation max_age_seconds")
    issue_count = (
        counters["near_expiry_pairs"]
        + counters["stale_pairs"]
        + counters["never_pairs"]
        + counters["future_pairs"]
        + counters["anomalous_pairs"]
    )
    # An ``ok`` status must never mask a positive health signal.  We permit an
    # explicit attention/degraded status when counters are currently clear so
    # callers can represent an operator-held state without widening the shape.
    if status == "ok" and issue_count:
        raise ValueError("observation health status masks a health issue")
    return {
        "status": status,
        "evaluated_at": evaluated_at,
        "stale_after_seconds": stale_after_seconds,
        **counters,
        "max_age_seconds": max_age_seconds,
    }


def _wallet_observation_health_breakdown(value: object) -> dict[str, Any]:
    """Validate and redact the bounded network/source health breakdown."""

    payload = _wallet_exact_dict(value, _WALLET_BREAKDOWN_FIELDS, "observation breakdown")
    group_by = payload["group_by"]
    if type(group_by) is not str or group_by not in {"network", "source"}:
        raise ValueError("observation breakdown dimension is invalid")
    evaluated_at = _wallet_timestamp(payload["evaluated_at"], "breakdown evaluated_at")
    stale_after_seconds = _wallet_nonnegative_int(
        payload["stale_after_seconds"], "breakdown stale_after_seconds"
    )
    if stale_after_seconds > WALLET_BALANCE_OBSERVATION_MAX_WINDOW_SECONDS:
        raise ValueError("breakdown stale_after_seconds exceeds the configured bound")
    status = payload["status"]
    if type(status) is not str or status not in _WALLET_HEALTH_STATUSES:
        raise ValueError("observation breakdown status is invalid")
    groups = payload["groups"]
    if type(groups) is not list or len(groups) > WALLET_BALANCE_OBSERVATION_MAX_GROUPS:
        raise ValueError("observation breakdown groups are invalid")
    total_groups = _wallet_nonnegative_int(payload["total_groups"], "breakdown total_groups")
    if total_groups < len(groups) or total_groups > WALLET_BALANCE_OBSERVATION_MAX_GROUPS:
        raise ValueError("observation breakdown total_groups is inconsistent")
    has_more = payload["has_more"]
    if type(has_more) is not bool or has_more != (total_groups > len(groups)):
        raise ValueError("observation breakdown pagination is inconsistent")
    severity = {"ok": 0, "attention": 1, "degraded": 2}
    projected: list[dict[str, Any]] = []
    previous_value: str | None = None
    saw_null = False
    for item in groups:
        group = _wallet_exact_dict(
            item, _WALLET_BREAKDOWN_GROUP_FIELDS, "observation breakdown group"
        )
        value_item = group["value"]
        if group_by == "network":
            if type(value_item) is not str or not value_item:
                raise ValueError("observation breakdown network value is invalid")
        elif value_item is not None and (type(value_item) is not str or not value_item):
            raise ValueError("observation breakdown source value is invalid")
        if value_item is None:
            if saw_null:
                raise ValueError("observation breakdown groups are duplicated")
            saw_null = True
        elif saw_null:
            raise ValueError("observation breakdown null group must be last")
        elif previous_value is not None and value_item <= previous_value:
            raise ValueError("observation breakdown groups are not sorted")
        elif value_item is not None:
            if group_by == "source" and _WALLET_OBSERVATION_SOURCE.fullmatch(value_item) is None:
                raise ValueError("observation breakdown source value is invalid")
            previous_value = value_item
        counters = {
            field: _wallet_nonnegative_int(group[field], f"breakdown {field}")
            for field in _WALLET_BREAKDOWN_COUNT_FIELDS
        }
        if counters["observed_pairs"] + counters["never_pairs"] != counters["active_pairs"]:
            raise ValueError("observation breakdown pair counters are inconsistent")
        if (
            counters["fresh_pairs"]
            + counters["near_expiry_pairs"]
            + counters["stale_pairs"]
            + counters["future_pairs"]
            != counters["observed_pairs"]
        ):
            raise ValueError("observation breakdown health counters are inconsistent")
        if counters["anomalous_pairs"] > counters["observed_pairs"]:
            raise ValueError("observation breakdown anomaly count is inconsistent")
        latest = _wallet_timestamp(
            group["latest_observed_at"], "breakdown latest_observed_at", nullable=True
        )
        max_age = group["max_age_seconds"]
        if max_age is not None:
            max_age = _wallet_nonnegative_int(max_age, "breakdown max_age_seconds")
        if counters["snapshot_count"] == 0 and (latest is not None or max_age is not None):
            raise ValueError("empty observation breakdown group has metadata")
        if counters["snapshot_count"] > 0 and latest is None:
            raise ValueError("non-empty observation breakdown group is missing timestamp")
        group_status = group["status"]
        if type(group_status) is not str or group_status not in _WALLET_HEALTH_STATUSES:
            raise ValueError("observation breakdown group status is invalid")
        issue_count = (
            counters["near_expiry_pairs"]
            + counters["stale_pairs"]
            + counters["never_pairs"]
            + counters["future_pairs"]
            + counters["anomalous_pairs"]
        )
        if group_status == "ok" and issue_count:
            raise ValueError("observation breakdown group status masks a health issue")
        projected.append(
            {
                "value": value_item,
                **counters,
                "latest_observed_at": latest,
                "max_age_seconds": max_age,
                "status": group_status,
            }
        )
    computed_status = max(
        (group["status"] for group in projected), key=severity.__getitem__, default="ok"
    )
    if status == "ok" and computed_status != "ok":
        raise ValueError("observation breakdown status masks a health issue")
    return {
        "group_by": group_by,
        "evaluated_at": evaluated_at,
        "stale_after_seconds": stale_after_seconds,
        "status": status,
        "groups": projected,
        "total_groups": total_groups,
        "has_more": has_more,
    }


def _wallet_acquisition_projection(value: object, subject_id: str) -> dict[str, Any]:
    """Validate the acquisition summary and redact all non-contract fields."""

    if type(value) is not dict:
        raise ValueError("wallet acquisition projection is invalid")
    if value == _wallet_unavailable():
        return _wallet_unavailable()
    keys = set(value)
    has_status = "status" in keys
    expected_fields = (
        _WALLET_ACQUISITION_FIELDS_WITH_STATUS if has_status else _WALLET_ACQUISITION_FIELDS
    )
    if keys != expected_fields:
        raise ValueError("wallet acquisition projection fields are invalid")
    payload = cast(dict[str, Any], value)
    if payload["subject_id"] != subject_id or type(payload["subject_id"]) is not str:
        raise ValueError("wallet acquisition subject is invalid")
    counts_payload = _wallet_exact_dict(
        payload["counts"], _WALLET_ACQUISITION_COUNT_FIELDS, "wallet acquisition counts"
    )
    counts = {
        state: _wallet_nonnegative_int(counts_payload[state], f"wallet acquisition {state}")
        for state in sorted(_WALLET_ACQUISITION_COUNT_FIELDS)
    }
    total = _wallet_nonnegative_int(payload["total"], "wallet acquisition total")
    active = _wallet_nonnegative_int(payload["active"], "wallet acquisition active")
    attention = _wallet_nonnegative_int(payload["attention"], "wallet acquisition attention")
    attempts = _wallet_nonnegative_int(payload["attempts"], "wallet acquisition attempts")
    expired_running = _wallet_nonnegative_int(
        payload["expired_running"], "wallet acquisition expired_running"
    )
    if total != sum(counts.values()):
        raise ValueError("wallet acquisition total is inconsistent")
    if active != counts["queued"] + counts["running"] + counts["retry_wait"]:
        raise ValueError("wallet acquisition active count is inconsistent")
    if attention != counts["unknown"] + counts["failed"]:
        raise ValueError("wallet acquisition attention count is inconsistent")
    if expired_running > counts["running"]:
        raise ValueError("wallet acquisition expired_running count is inconsistent")
    next_attempt_at = _wallet_timestamp(
        payload["next_attempt_at"], "wallet acquisition next_attempt_at", nullable=True
    )
    computed_status = "attention" if attention or expired_running else "ok"
    if has_status:
        status = payload["status"]
        if type(status) is not str or status not in _WALLET_HEALTH_STATUSES:
            raise ValueError("wallet acquisition status is invalid")
        if status == "ok" and computed_status != "ok":
            raise ValueError("wallet acquisition status masks a health issue")
    else:
        status = computed_status
    return {
        "subject_id": subject_id,
        "status": status,
        "counts": counts,
        "total": total,
        "active": active,
        "attention": attention,
        "attempts": attempts,
        "expired_running": expired_running,
        "next_attempt_at": next_attempt_at,
    }


def _wallet_status_summary_projection(value: object, subject_id: str) -> dict[str, Any]:
    """Validate the WalletStore summary and return a safe, fixed-size copy."""

    payload = _wallet_exact_dict(value, _WALLET_SUMMARY_FIELDS, "wallet status")
    if type(payload["subject_id"]) is not str or payload["subject_id"] != subject_id:
        raise ValueError("wallet status subject is invalid")
    resources_payload = _wallet_exact_dict(
        payload["resources"], frozenset(_WALLET_RESOURCE_NAMES), "wallet resources"
    )
    resources = {
        name: _wallet_resource_counts(resources_payload[name], f"wallet resources.{name}")
        for name in _WALLET_RESOURCE_NAMES
    }
    history_payload = _wallet_exact_dict(
        payload["balance_history"], _WALLET_BALANCE_HISTORY_FIELDS, "wallet balance history"
    )
    snapshots = _wallet_nonnegative_int(history_payload["snapshots"], "wallet history snapshots")
    latest_observed_at = _wallet_timestamp(
        history_payload["latest_observed_at"], "wallet latest_observed_at", nullable=True
    )
    latest_created_at = _wallet_timestamp(
        history_payload["latest_created_at"], "wallet latest_created_at", nullable=True
    )
    observation = _wallet_observation_health(history_payload["observation_health"])
    observed_pairs = observation["observed_pairs"]
    if type(observed_pairs) is not int or observed_pairs > snapshots:
        raise ValueError("wallet history observation count is inconsistent")
    if snapshots == 0:
        if latest_observed_at is not None or latest_created_at is not None or observed_pairs != 0:
            raise ValueError("empty wallet history has non-empty observation metadata")
    elif latest_observed_at is None or latest_created_at is None:
        raise ValueError("non-empty wallet history is missing timestamp metadata")
    return {
        "subject_id": subject_id,
        "resources": resources,
        "balance_history": {
            "snapshots": snapshots,
            "latest_observed_at": latest_observed_at,
            "latest_created_at": latest_created_at,
            "observation_health": observation,
        },
    }


def _wallet_acquisition_run_payload(record: Any) -> dict[str, Any]:
    """Return operator-safe acquisition metadata without lease credentials."""
    payload = dict(record.__dict__)
    payload.pop("claim_token", None)
    payload.pop("lease_owner", None)
    return payload


def _wallet_acquisition_attempt_payload(record: Any) -> dict[str, Any]:
    """Return operator-safe attempt metadata without lease credentials."""
    payload = dict(record.__dict__)
    payload.pop("claim_token", None)
    payload.pop("lease_owner", None)
    return payload
