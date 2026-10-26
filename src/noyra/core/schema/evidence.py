"""Evidence definitions; execution remains in core.database."""

from __future__ import annotations

import sqlite3
from typing import Any

from noyra.core.types import content_hash, strict_int
from noyra.core.wallet_schema import validate_wallet_timestamp


def goal_origin_evidence_hash(
    goal_id: str, subject_id: str, origin: str, created_at: str, evidence: str
) -> str:
    """Bind observed goal provenance without upgrading legacy ancestry claims."""
    return content_hash(
        {
            "format": "noyra-goal-origin/v1",
            "goal_id": goal_id,
            "subject_id": subject_id,
            "origin": origin,
            "created_at": created_at,
            "evidence": evidence,
        }
    )


def action_state_hash(row: sqlite3.Row) -> str:
    """Hash the immutable action state used by action revision records."""
    return content_hash(
        {
            "action_id": row["action_id"],
            "subject_id": row["subject_id"],
            "goal_id": row["goal_id"],
            "project_id": row["project_id"],
            "phase_id": row["phase_id"],
            "strategy_id": row["strategy_id"],
            "action_type": row["action_type"],
            "tool": row["tool"],
            "target": row["target"],
            "input_hash": row["input_hash"],
            "idempotency_key": row["idempotency_key"],
            "expected_outcome": row["expected_outcome"],
            "side_effect": bool(row["side_effect"]),
            "status": row["status"],
            "retry_count": int(row["retry_count"]),
            "resource_cost_json": row["resource_cost_json"],
            "result_json": row["result_json"],
            "prepared_at": row["prepared_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }
    )


def public_post_identity_hash(row: Any) -> str:
    """Hash the immutable public-post envelope, excluding moderation state."""
    return content_hash(
        {
            "post_id": row["post_id"],
            "subject_id": row["subject_id"],
            "kind": row["kind"],
            "title": row["title"],
            "content_hash": row["content_hash"],
            "author_label": row["author_label"],
            "author_provenance": row["author_provenance"],
            "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"],
        }
    )


def behavior_log_state_hash(row: Any) -> str:
    """Hash one immutable behavior-log revision payload."""
    return content_hash(
        {
            "log_id": row["log_id"],
            "action_id": row["action_id"],
            "subject_id": row["subject_id"],
            "revision_number": int(row["revision_number"]),
            "occurred_at": row["occurred_at"],
            "action_type": row["action_type"],
            "public_goal_reference": row["public_goal_reference"],
            "tool": row["tool"],
            "public_target": row["public_target"],
            "result_status": row["result_status"],
            "side_effect_summary": row["side_effect_summary"],
            "resource_summary": row["resource_summary"],
            "public_explanation": row["public_explanation"],
            "redaction_reason": row["redaction_reason"],
            "reason": row["reason"],
        }
    )


def wallet_submission_state_hash(
    *,
    submission_id: str,
    bounty_id: str,
    subject_id: str,
    counterparty: str,
    content: str,
    evidence_json: str,
    recipient_address: str,
    idempotency_key: str,
    consent_version: int,
    status: str,
    decision_reason: str | None,
) -> str:
    """Hash the complete durable bounty-submission state."""
    return content_hash(
        {
            "submission_id": submission_id,
            "bounty_id": bounty_id,
            "subject_id": subject_id,
            "counterparty": counterparty,
            "content": content,
            "evidence_json": evidence_json,
            "recipient_address": recipient_address,
            "idempotency_key": idempotency_key,
            "consent_version": consent_version,
            "status": status,
            "decision_reason": decision_reason,
        }
    )


def wallet_payment_policy_state_hash(
    *,
    subject_id: str,
    mode: str,
    allowed_network_ids_json: str,
    allowed_asset_ids_json: str,
    per_order_limit: str,
    daily_limit: str,
    monthly_limit: str,
    daily_order_limit: int,
    monthly_order_limit: int,
    min_balance: str,
    max_observation_age_seconds: int,
    automatic_max_amount: str,
    anomaly_block: int | bool,
    emergency_paused: int | bool,
    policy_version: int,
    updated_at: str,
    recipient_allowlist_enabled: int | bool = False,
    allowed_recipient_addresses_json: str = "[]",
    automation_enabled: int | bool | None = None,
) -> str:
    """Hash the complete durable wallet payment policy state."""
    validate_wallet_timestamp(updated_at)
    return content_hash(
        {
            "subject_id": subject_id,
            "mode": mode,
            "allowed_network_ids_json": allowed_network_ids_json,
            "allowed_asset_ids_json": allowed_asset_ids_json,
            "per_order_limit": per_order_limit,
            "daily_limit": daily_limit,
            "monthly_limit": monthly_limit,
            "daily_order_limit": strict_int(daily_order_limit),
            "monthly_order_limit": strict_int(monthly_order_limit),
            "min_balance": min_balance,
            "max_observation_age_seconds": strict_int(max_observation_age_seconds),
            "automatic_max_amount": automatic_max_amount,
            "anomaly_block": int(anomaly_block),
            "emergency_paused": int(emergency_paused),
            # Older callers derived the switch from the payment mode.  Keep
            # that compatibility behavior while new durable call sites pass
            # the explicit audited value.
            "automation_enabled": int(
                mode == "automatic" if automation_enabled is None else automation_enabled
            ),
            "recipient_allowlist_enabled": int(recipient_allowlist_enabled),
            "allowed_recipient_addresses_json": allowed_recipient_addresses_json,
            "policy_version": strict_int(policy_version),
            "updated_at": updated_at,
        }
    )


def legacy_wallet_payment_policy_state_hash_v65(
    *,
    subject_id: str,
    mode: str,
    allowed_network_ids_json: str,
    allowed_asset_ids_json: str,
    per_order_limit: str,
    daily_limit: str,
    monthly_limit: str,
    daily_order_limit: int,
    monthly_order_limit: int,
    min_balance: str,
    max_observation_age_seconds: int,
    automatic_max_amount: str,
    anomaly_block: int | bool,
    emergency_paused: int | bool,
    policy_version: int,
    updated_at: str,
) -> str:
    """Hash contract used immediately before schema 66 added recipient policy."""
    validate_wallet_timestamp(updated_at)
    return content_hash(
        {
            "subject_id": subject_id,
            "mode": mode,
            "allowed_network_ids_json": allowed_network_ids_json,
            "allowed_asset_ids_json": allowed_asset_ids_json,
            "per_order_limit": per_order_limit,
            "daily_limit": daily_limit,
            "monthly_limit": monthly_limit,
            "daily_order_limit": strict_int(daily_order_limit),
            "monthly_order_limit": strict_int(monthly_order_limit),
            "min_balance": min_balance,
            "max_observation_age_seconds": strict_int(max_observation_age_seconds),
            "automatic_max_amount": automatic_max_amount,
            "anomaly_block": int(anomaly_block),
            "emergency_paused": int(emergency_paused),
            "policy_version": strict_int(policy_version),
            "updated_at": updated_at,
        }
    )
