"""Wallet payments HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

from noyra.core.errors import IntegrityError, InvalidTransitionError, NotFoundError
from noyra.wallet import PaymentPolicyInput, WalletExecutionError

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def wallet_order_operation(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    payload = self._wallet_payload()
    if payload is None:
        return
    if not isinstance(payload, dict):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_order_operation"})
        return
    parts = self.path.removeprefix("/api/admin/wallet-orders/").split("/")
    if len(parts) != 2 or not all(parts):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_order_operation"})
        return
    order_id, operation = parts
    execution_record: Any
    if operation in {"execute", "retry", "refund"}:
        engine = owner.wallet_execution
        if engine is None:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "wallet_execution_unavailable"},
                retry_after=60,
            )
            return
        try:
            actor = self._actor()
            if operation == "execute":
                allowed = {"gas_limit", "max_fee_per_gas", "nonce"}
                if set(payload) - allowed:
                    raise ValueError("invalid wallet execute payload")
                kwargs: dict[str, Any] = {}
                for key in allowed:
                    if key in payload:
                        value = payload[key]
                        if key in {"gas_limit", "nonce"} and (type(value) is not int):
                            raise ValueError("wallet execution quantity is invalid")
                        if key == "max_fee_per_gas" and not isinstance(value, str):
                            raise ValueError("wallet execution fee is invalid")
                        kwargs[key] = value
                execution_record = engine.execute_order(
                    order_id, owner.kernel.subject_id, actor=actor, **kwargs
                )
            elif operation == "retry":
                if set(payload) != {"reason"} or not isinstance(payload.get("reason"), str):
                    raise ValueError("wallet retry reason is required")
                execution_record = engine.retry_unknown(
                    order_id,
                    owner.kernel.subject_id,
                    actor=actor,
                    reason=payload["reason"],
                )
            else:
                if set(payload) != {"reason"} or not isinstance(payload.get("reason"), str):
                    raise ValueError("wallet refund reason is required")
                execution_record = cast(
                    Any,
                    engine.refund(
                        order_id,
                        owner.kernel.subject_id,
                        actor=actor,
                        reason=payload["reason"],
                    ),
                )
            self._json(HTTPStatus.OK, execution_record.__dict__)
        except NotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_order_not_found"})
        except IntegrityError:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "wallet_execution_integrity_unavailable"},
                retry_after=60,
            )
        except InvalidTransitionError:
            self._json(HTTPStatus.CONFLICT, {"error": "wallet_order_transition_conflict"})
        except (WalletExecutionError, ValueError, TypeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_order_execution"})
        return
    try:
        if operation == "confirm":
            order_record = owner.wallet_economy.confirm_order(
                order_id, owner.kernel.subject_id, actor=self._actor()
            )
        elif operation == "reject":
            order_record = owner.wallet_economy.reject_order(
                order_id,
                owner.kernel.subject_id,
                actor=self._actor(),
                reason=str(payload.get("reason", "rejected")),
            )
        elif operation == "cancel":
            order_record = owner.wallet_economy.cancel_order(
                order_id,
                owner.kernel.subject_id,
                actor=self._actor(),
                reason=str(payload.get("reason", "cancelled")),
            )
        else:
            raise ValueError
        self._json(HTTPStatus.OK, order_record.__dict__)
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_order_not_found"})
    except Exception:
        self._json(HTTPStatus.CONFLICT, {"error": "wallet_order_transition_conflict"})


def wallet_execution_operation(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    payload = self._wallet_payload()
    if payload is None:
        return
    engine = owner.wallet_execution
    if engine is None:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_execution_unavailable"},
            retry_after=60,
        )
        return
    parts = self.path.removeprefix("/api/admin/wallet-executions/").split("/")
    if len(parts) != 2 or not parts[0] or parts[1] != "receipt":
        self._discard_small_request_body()
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_path"})
        return
    if payload:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_receipt_request"})
        return
    try:
        execution_record = engine.poll_receipt(
            parts[0], owner.kernel.subject_id, actor=self._actor()
        )
        self._json(HTTPStatus.OK, execution_record.__dict__)
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_execution_not_found"})
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_execution_integrity_unavailable"},
            retry_after=60,
        )
    except InvalidTransitionError:
        self._json(HTTPStatus.CONFLICT, {"error": "wallet_execution_transition_conflict"})
    except (WalletExecutionError, ValueError, TypeError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_receipt_request"})


def recover_wallet_executions(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    payload = self._wallet_payload()
    if payload is None:
        return
    if set(payload) - {"limit"}:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_recovery"})
        return
    engine = owner.wallet_execution
    if engine is None:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_execution_unavailable"},
            retry_after=60,
        )
        return
    limit_value = payload.get("limit", 100)
    if type(limit_value) is not int:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_recovery"})
        return
    try:
        records = engine.recover_inflight(
            owner.kernel.subject_id, actor=self._actor(), limit=limit_value
        )
        self._json(HTTPStatus.OK, [record.__dict__ for record in records])
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_execution_integrity_unavailable"},
            retry_after=60,
        )
    except (ValueError, TypeError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_recovery"})


def update_wallet_policy(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    payload = self._wallet_payload()
    if payload is None:
        return
    if not isinstance(payload, dict) or "expected_version" not in payload:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_policy"})
        return
    try:
        expected = int(payload.pop("expected_version"))
        record = owner.wallet_economy.update_policy(
            owner.kernel.subject_id,
            PaymentPolicyInput.model_validate(payload),
            expected_version=expected,
            actor=self._actor(),
        )
        self._json(HTTPStatus.OK, record.__dict__)
    except Exception as error:
        self._json(
            HTTPStatus.CONFLICT if "version" in str(error) else HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_policy"},
        )


def wallet_payload(self: RequestHandler, owner: NoyraHTTPServer) -> dict[str, Any] | None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return None
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return None
    return self._request_json()
