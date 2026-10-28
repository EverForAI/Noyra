"""Wallet automation HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from noyra.core.release_assurance import activation_status
from noyra.core.types import content_hash
from noyra.wallet import PaymentPolicyInput

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def wallet_automation_projection(self: RequestHandler, owner: NoyraHTTPServer) -> dict[str, Any]:
    policy = owner.wallet_economy.get_policy(owner.kernel.subject_id)
    pending: dict[str, int] = {}
    latest_failure: dict[str, Any] | None = None
    try:
        with owner.kernel.database.read_transaction() as connection:
            rows = connection.execute(
                "SELECT error_code, COUNT(*) AS count FROM wallet_payment_executions "
                "WHERE subject_id=? AND status IN ('unknown','failed') "
                "GROUP BY error_code",
                (owner.kernel.subject_id,),
            ).fetchall()
            latest = connection.execute(
                "SELECT execution_id,status,error_code,updated_at "
                "FROM wallet_payment_executions "
                "WHERE subject_id=? AND status IN ('unknown','failed') "
                "ORDER BY updated_at DESC,execution_id DESC LIMIT 1",
                (owner.kernel.subject_id,),
            ).fetchone()
        from noyra.wallet.execution import WalletPaymentExecutionEngine

        for row in rows:
            code = WalletPaymentExecutionEngine.normalize_reason_code(row["error_code"])
            pending[code] = pending.get(code, 0) + int(row["count"])
        if latest is not None:
            reason_code = WalletPaymentExecutionEngine.normalize_reason_code(latest["error_code"])
            latest_failure = {
                "execution_id": str(latest["execution_id"]),
                "status": str(latest["status"]),
                "reason_code": reason_code,
                "updated_at": str(latest["updated_at"]),
                "requires_reconciliation": (
                    latest["status"] == "unknown" or reason_code == "reconcile_required"
                ),
            }
    except Exception:
        pending = {}
        latest_failure = None
    active = 0
    try:
        with owner.kernel.database.read_transaction() as connection:
            active = int(
                connection.execute(
                    """
                                SELECT COUNT(*) FROM wallet_payment_executions
                                WHERE subject_id=? AND status IN ('signing','broadcast','unknown')
                                """,
                    (owner.kernel.subject_id,),
                ).fetchone()[0]
            )
    except Exception:
        pass
    evidence = activation_status()
    evidence_blocked = owner.settings.profile == "production" and evidence["status"] != "verified"
    return {
        "automation_enabled": bool(policy.automation_enabled),
        "mode": policy.mode,
        "emergency_paused": bool(policy.emergency_paused),
        "release_evidence": evidence,
        "policy_version": policy.policy_version,
        "per_order_limit": policy.per_order_limit,
        "daily_limit": policy.daily_limit,
        "active_executions": active,
        "pending_reason_counts": pending,
        "latest_failure": latest_failure,
        "status": (
            "paused"
            if policy.emergency_paused
            else "evidence_required"
            if evidence_blocked and policy.automation_enabled and policy.mode == "automatic"
            else "enabled"
            if policy.automation_enabled and policy.mode == "automatic"
            else "disabled"
        ),
    }


def update_wallet_automation(
    self: RequestHandler, owner: NoyraHTTPServer, payload: object | None = None
) -> None:
    if payload is None:
        payload = self._wallet_payload()
    if payload is None:
        return
    if not isinstance(payload, dict):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_automation"})
        return
    try:
        payload = dict(payload)
        expected = payload.pop("expected_version")
        if type(expected) is not int or expected < 1:
            raise ValueError("version")
        reason = payload.pop("reason", "管理员更新自动付款设置")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("reason")
        reason = reason.strip()
        idem = payload.pop("idempotency_key", "")
        if not isinstance(idem, str) or not idem.strip() or len(idem) > 128:
            raise ValueError("idempotency_key")
        idem = idem.strip()
        if not payload or set(payload) - {
            "mode",
            "automation_enabled",
            "emergency_paused",
        }:
            raise ValueError("unknown fields")
        for key in ("automation_enabled", "emergency_paused"):
            if key in payload and type(payload[key]) is not bool:
                raise ValueError(key)
        digest = content_hash(
            {
                "actor": self._actor(),
                "expected_version": expected,
                "reason": reason,
                **payload,
            }
        )
        current = owner.wallet_economy.get_policy(owner.kernel.subject_id)
        allowed = {
            "mode": current.mode,
            "allowed_network_ids": list(current.allowed_network_ids),
            "allowed_asset_ids": list(current.allowed_asset_ids),
            "per_order_limit": current.per_order_limit,
            "daily_limit": current.daily_limit,
            "monthly_limit": current.monthly_limit,
            "daily_order_limit": current.daily_order_limit,
            "monthly_order_limit": current.monthly_order_limit,
            "min_balance": current.min_balance,
            "max_observation_age_seconds": current.max_observation_age_seconds,
            "automatic_max_amount": current.automatic_max_amount,
            "anomaly_block": current.anomaly_block,
            "emergency_paused": payload.pop("emergency_paused", current.emergency_paused),
            "automation_enabled": payload.pop("automation_enabled", current.automation_enabled),
            "recipient_allowlist_enabled": current.recipient_allowlist_enabled,
            "allowed_recipient_addresses": list(current.allowed_recipient_addresses),
        }
        if "mode" in payload:
            allowed["mode"] = payload.pop("mode")
        if payload:
            raise ValueError("unknown fields")
        owner.wallet_economy.update_policy(
            owner.kernel.subject_id,
            PaymentPolicyInput.model_validate(allowed),
            expected_version=expected,
            actor=self._actor(),
            automation_request={
                "idempotency_key": idem,
                "reason": reason,
                "request_digest": digest,
            },
        )
        self._json(HTTPStatus.OK, self._wallet_automation_projection())
    except Exception as error:
        self._json(
            HTTPStatus.CONFLICT if "conflict" in str(error) else HTTPStatus.BAD_REQUEST,
            {
                "error": "production_release_evidence_required"
                if str(error) == "production_release_evidence_required"
                else "invalid_wallet_automation"
            },
        )


def set_wallet_automation_pause(self: RequestHandler, owner: NoyraHTTPServer, paused: bool) -> None:
    payload = self._wallet_payload()
    if payload is None:
        return
    if not isinstance(payload, dict):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_automation"})
        return
    if set(payload) - {"expected_version", "reason", "idempotency_key"}:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_automation"})
        return
    payload["emergency_paused"] = paused
    payload.setdefault("idempotency_key", f"pause-{paused}-{time.time_ns()}")
    payload.setdefault("reason", "管理员紧急暂停" if paused else "管理员解除紧急暂停")
    self._update_wallet_automation(payload)
