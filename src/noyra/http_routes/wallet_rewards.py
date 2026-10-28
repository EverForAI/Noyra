"""Wallet rewards HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from dataclasses import asdict
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from noyra.core.errors import IntegrityError, NotFoundError
from noyra.wallet import (
    BountyInput,
    RewardEvidenceDecisionInput,
    RewardIncidentResolutionInput,
    RewardPublicSubmissionInput,
    RewardWorkflowInput,
    SubmissionInput,
    WalletExecutionError,
)

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def create_wallet_bounty(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    payload = self._wallet_payload()
    if payload is None:
        return
    try:
        record = owner.wallet_economy.create_bounty(
            owner.kernel.subject_id,
            BountyInput.model_validate(payload),
            actor=self._actor(),
        )
        self._json(HTTPStatus.CREATED, asdict(record))
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_reference_not_found"})
    except Exception as error:
        self._json(
            HTTPStatus.CONFLICT if "idempotency" in str(error) else HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_bounty"},
        )


def create_wallet_reward(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    payload = self._wallet_payload()
    if payload is None:
        return
    if set(payload) - {
        "assistance_request_id",
        "acceptance_criteria",
        "network_id",
        "asset_id",
        "reward_amount",
        "opens_at",
        "expires_at",
        "max_submissions",
        "reward_slots",
        "idempotency_key",
    }:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward"})
        return
    try:
        record = owner.wallet_rewards.create(
            owner.kernel.subject_id,
            RewardWorkflowInput.model_validate(payload),
            actor=self._actor(),
        )
        self._json(HTTPStatus.CREATED, record.__dict__)
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_reward_reference_not_found"})
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_reward_integrity_unavailable"},
            retry_after=60,
        )
    except Exception as error:
        self._json(
            HTTPStatus.CONFLICT if "idempotency" in str(error) else HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_reward"},
        )


def wallet_reward_operation(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    parts = self.path.removeprefix("/api/admin/wallet-rewards/").split("/")
    if len(parts) != 2 or not all(parts):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_operation"})
        return
    resource_id, operation = parts
    payload = self._request_json()
    if payload is None:
        return
    if operation == "publish" and payload:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_operation"})
        return
    if operation == "resume" and set(payload) != {"reason"}:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_operation"})
        return
    try:
        if operation == "publish":
            record: Any = owner.wallet_rewards.publish(
                resource_id, owner.kernel.subject_id, actor=self._actor()
            )
        elif operation == "resume":
            reason = payload.get("reason")
            if not isinstance(reason, str):
                raise ValueError("resume reason is invalid")
            record = owner.wallet_rewards.resume(
                resource_id,
                owner.kernel.subject_id,
                actor=self._actor(),
                reason=reason,
            )
        else:
            raise ValueError("unknown reward operation")
        self._json(HTTPStatus.OK, record.__dict__)
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_reward_not_found"})
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_reward_integrity_unavailable"},
            retry_after=60,
        )
    except Exception:
        self._json(HTTPStatus.CONFLICT, {"error": "wallet_reward_transition_conflict"})


def decide_wallet_reward_submission(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    submission_id = self.path.removeprefix("/api/admin/wallet-reward-submissions/").removesuffix(
        "/decide"
    )
    if not submission_id or "/" in submission_id:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_reward_submission"},
        )
        return
    payload = self._request_json()
    if payload is None:
        return
    try:
        record = owner.wallet_rewards.decide(
            submission_id,
            owner.kernel.subject_id,
            RewardEvidenceDecisionInput.model_validate(payload),
            actor=self._actor(),
        )
        self._json(HTTPStatus.OK, asdict(record))
    except NotFoundError:
        self._json(
            HTTPStatus.NOT_FOUND,
            {"error": "wallet_reward_submission_not_found"},
        )
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_reward_integrity_unavailable"},
            retry_after=60,
        )
    except Exception as error:
        self._json(
            HTTPStatus.CONFLICT if "idempotency" in str(error) else HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_reward_decision"},
        )


def resolve_wallet_reward_incident(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    incident_id = self.path.removeprefix("/api/admin/wallet-reward-incidents/").removesuffix(
        "/resolve"
    )
    if not incident_id or "/" in incident_id:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_incident"})
        return
    payload = self._request_json()
    if payload is None:
        return
    if set(payload) != {"resolution", "idempotency_key"}:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_incident"})
        return
    try:
        record = owner.wallet_rewards.resolve_incident(
            incident_id,
            owner.kernel.subject_id,
            RewardIncidentResolutionInput.model_validate(payload),
            actor=self._actor(),
        )
        self._json(HTTPStatus.OK, record.__dict__)
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_reward_incident_not_found"})
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_reward_integrity_unavailable"},
            retry_after=60,
        )
    except Exception as error:
        self._json(
            (HTTPStatus.CONFLICT if "idempotency" in str(error) else HTTPStatus.BAD_REQUEST),
            {"error": "invalid_wallet_reward_incident_resolution"},
        )


def submit_wallet_reward(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    workflow_id = self.path.removeprefix("/api/wallet-rewards/").removesuffix("/submissions")
    if not workflow_id or "/" in workflow_id:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward"})
        return
    try:
        record = owner.wallet_rewards.submit_public(
            workflow_id,
            owner.kernel.subject_id,
            RewardPublicSubmissionInput.model_validate(payload),
            client_ip=str(self.client_address[0]),
        )
        self._json(HTTPStatus.CREATED, asdict(record))
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_reward_not_found"})
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_reward_integrity_unavailable"},
            retry_after=60,
        )
    except Exception as error:
        self._json(
            HTTPStatus.CONFLICT if "idempotency" in str(error) else HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_reward_submission"},
        )


def execute_wallet_rewards(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    payload = self._wallet_payload()
    if payload is None:
        return
    if set(payload) - {"limit"}:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_execution"})
        return
    limit = payload.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= 100:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_execution"})
        return
    try:
        records = owner.wallet_rewards.execute_ready(
            owner.kernel.subject_id, actor=self._actor(), limit=limit
        )
        self._json(HTTPStatus.OK, [asdict(record) for record in records])
    except WalletExecutionError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_execution_unavailable"},
            retry_after=60,
        )
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_reward_integrity_unavailable"},
            retry_after=60,
        )


def wallet_bounty_operation(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    parts = self.path.removeprefix("/api/admin/wallet-bounties/").split("/")
    if len(parts) != 2 or not all(parts):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_bounty_operation"})
        return
    bid, op = parts
    payload = self._request_json()
    if payload is None:
        return
    try:
        record = {
            "publish": owner.wallet_economy.publish_bounty,
            "close": owner.wallet_economy.close_bounty,
            "cancel": owner.wallet_economy.cancel_bounty,
        }[op](bid, owner.kernel.subject_id, actor=self._actor())
        self._json(HTTPStatus.OK, record.__dict__)
    except KeyError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_bounty_operation"})
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_bounty_not_found"})
    except Exception:
        self._json(HTTPStatus.CONFLICT, {"error": "wallet_bounty_transition_conflict"})


def submit_wallet_bounty(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    bid = self.path.removeprefix("/api/bounties/").removesuffix("/submissions")
    if not bid or "/" in bid:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_bounty"})
        return
    try:
        record = owner.wallet_economy.submit(
            bid,
            owner.kernel.subject_id,
            SubmissionInput.model_validate(payload),
            client_ip=str(self.client_address[0]),
        )
        self._json(HTTPStatus.CREATED, record.__dict__)
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_bounty_not_found"})
    except Exception as error:
        self._json(
            HTTPStatus.CONFLICT if "idempotency" in str(error) else HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_submission"},
        )


def wallet_submission_operation(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    parts = self.path.removeprefix("/api/admin/wallet-submissions/").split("/")
    if len(parts) != 2:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_submission_operation"})
        return
    sid, op = parts
    payload = self._request_json()
    if payload is None:
        return
    try:
        if op == "accept":
            record = owner.wallet_economy.decide_submission(
                sid,
                owner.kernel.subject_id,
                accepted=True,
                reason=str(payload.get("reason", "accepted")),
                actor=self._actor(),
            )
        elif op == "reject":
            record = owner.wallet_economy.decide_submission(
                sid,
                owner.kernel.subject_id,
                accepted=False,
                reason=str(payload.get("reason", "rejected")),
                actor=self._actor(),
            )
        elif op == "withdraw":
            record = owner.wallet_economy.withdraw_submission(sid, owner.kernel.subject_id)
        else:
            raise ValueError
        self._json(HTTPStatus.OK, record.__dict__)
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_submission_not_found"})
    except Exception:
        self._json(HTTPStatus.CONFLICT, {"error": "wallet_submission_transition_conflict"})
