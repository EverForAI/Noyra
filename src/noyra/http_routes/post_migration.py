"""Post migration HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import os
from dataclasses import asdict
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from noyra.core.errors import NotFoundError
from noyra.migration import MigrationExecutionError, RecoveryRequest, TargetChallenge

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def admin_migration_policy(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    payload = self._request_json()
    if payload is None:
        return True
    try:
        expected_revision = payload.pop("expected_revision")
        current_policy = owner.migration_store.read_policy(owner.kernel.subject_id)
        requested_mode = payload.get("approval_mode", current_policy.approval_mode)
        requested_enabled = payload.get("enabled", current_policy.enabled)
        requested_wallet_mode = payload.get("wallet_mode", current_policy.wallet_mode)
        requested_local_wallet = payload.get(
            "local_wallet_transfer_enabled",
            current_policy.local_wallet_transfer_enabled,
        )
        entering_policy_auto = bool(
            requested_enabled
            and requested_mode == "policy_auto"
            and (current_policy.approval_mode != "policy_auto" or not current_policy.enabled)
        )
        entering_local_wallet = bool(
            requested_enabled
            and requested_wallet_mode == "local_wallet_transfer"
            and requested_local_wallet
            and (
                current_policy.wallet_mode != "local_wallet_transfer"
                or not current_policy.local_wallet_transfer_enabled
            )
        )
        if entering_policy_auto:
            if payload.pop("confirm_policy_auto", False) is not True:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "migration_policy_confirmation_required"},
                )
                return True
        else:
            payload.pop("confirm_policy_auto", None)
        if entering_local_wallet:
            if payload.pop("confirm_local_wallet_transfer", False) is not True:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "migration_local_wallet_confirmation_required"},
                )
                return True
        else:
            payload.pop("confirm_local_wallet_transfer", None)
        updated = owner.migration_store.update_policy(
            owner.kernel.subject_id,
            expected_revision,
            payload,
            self._actor(),
        )
    except KeyError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "expected_revision_required"})
        return True
    except Exception as error:
        if error.__class__.__name__ == "MigrationPolicyConflictError":
            self._json(HTTPStatus.CONFLICT, {"error": "migration_policy_conflict"})
        else:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_migration_policy"},
            )
        return True
    self._json(HTTPStatus.OK, updated.__dict__)
    return True
    return False


def admin_migration_targets(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    payload = self._request_json()
    if payload is None:
        return True
    try:
        target = owner.migration_targets.register(
            owner.kernel.subject_id,
            target_id=str(payload["target_id"]),
            public_key=str(payload["public_key"]),
            recipient_public_key=(
                str(payload["recipient_public_key"])
                if payload.get("recipient_public_key") is not None
                else None
            ),
            endpoint=str(payload["endpoint"]),
            capabilities=payload.get("capabilities", {}),
            region=payload.get("region"),
            provider=payload.get("provider"),
            release_sha=str(payload["release_sha"]),
            os_arch=str(payload["os_arch"]),
            encrypted_volume=payload.get("encrypted_volume") is True,
            actor=self._actor(),
        )
    except Exception:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_migration_target"})
        return True
    if payload.get("channel_token"):
        from noyra.migration.settings import write_target_token

        try:
            write_target_token(
                Path(
                    os.getenv(
                        "NOYRA_MIGRATION_TARGET_TOKEN_DIR",
                        str(owner.settings.data_dir / "secrets" / "migration-targets"),
                    )
                ),
                target.target_id,
                payload["channel_token"],
            )
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "migration_token_invalid"})
            return True
    self._json(HTTPStatus.CREATED, target.__dict__)
    return True
    return False


def admin_migration_targets_challenge(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    target_id = (
        self.path.removeprefix("/api/admin/migration/targets/")
        .removesuffix("/challenge")
        .strip("/")
    )
    payload = self._request_json()
    if payload is None:
        return True
    try:
        challenge = owner.migration_targets.issue_challenge(
            target_id,
            source_epoch=str(
                payload.get(
                    "source_epoch",
                    f"runtime-{owner.kernel.lifecycle.current().version}",
                )
            ),
        )
    except (NotFoundError, ValueError):
        self._json(
            HTTPStatus.CONFLICT,
            {"error": "migration_target_challenge_failed"},
        )
        return True
    if payload.get("automatic") is True:
        try:
            with owner.kernel.database.connection() as connection:
                target_row = connection.execute(
                    "SELECT endpoint FROM migration_targets WHERE target_id=? AND subject_id=?",
                    (target_id, owner.kernel.subject_id),
                ).fetchone()
            if target_row is None:
                raise ValueError("unknown target")
            response = owner.migration_http_executor.transport.request(
                str(target_row["endpoint"]).rstrip("/") + "/v1/challenge",
                asdict(challenge),
                owner._migration_http_token(SimpleNamespace(target_id=target_id)),
            )
            evidence = owner.migration_targets.attest(
                target_id,
                challenge,
                str(response["signature"]),
                actor=self._actor(),
            )
        except (KeyError, ValueError, MigrationExecutionError):
            self._json(
                HTTPStatus.CONFLICT,
                {"error": "migration_target_attestation_failed"},
            )
            return True
        self._json(HTTPStatus.OK, asdict(evidence))
        return True
    self._json(HTTPStatus.OK, challenge.__dict__)
    return True
    return False


def admin_migration_targets_attest(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    target_id = (
        self.path.removeprefix("/api/admin/migration/targets/").removesuffix("/attest").strip("/")
    )
    payload = self._request_json()
    if payload is None:
        return True
    try:
        evidence = owner.migration_targets.attest(
            target_id,
            TargetChallenge(
                nonce=str(payload["nonce"]),
                expires_at=str(payload["expires_at"]),
                source_epoch=str(payload["source_epoch"]),
                target_id=(str(payload["target_id"]) if payload.get("target_id") else None),
                endpoint_origin=(
                    str(payload["endpoint_origin"]) if payload.get("endpoint_origin") else None
                ),
            ),
            str(payload["signature"]),
            actor=self._actor(),
        )
    except (KeyError, NotFoundError, ValueError):
        self._json(
            HTTPStatus.CONFLICT,
            {"error": "migration_target_attestation_failed"},
        )
        return True
    self._json(HTTPStatus.OK, evidence.__dict__)
    return True
    return False


def admin_migration_targets_revoke(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    target_id = (
        self.path.removeprefix("/api/admin/migration/targets/").removesuffix("/revoke").strip("/")
    )
    payload = self._request_json()
    if payload is None:
        return True
    try:
        owner.migration_targets.revoke(
            target_id,
            reason=str(payload.get("reason", "revoked by operator")),
            actor=self._actor(),
        )
    except Exception:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "migration_target_revoke_failed"},
        )
        return True
    self._json(HTTPStatus.OK, {"target_id": target_id, "status": "revoked"})
    return True
    return False


def admin_migration_proposals_approve(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    proposal_id = (
        self.path.removeprefix("/api/admin/migration/proposals/")
        .removesuffix("/approve")
        .strip("/")
    )
    payload = self._request_json()
    if payload is None:
        return True
    try:
        actor, identity_binding, reviewer_principal = self._approval_identity()
        task = owner.migration_manager.approve(
            proposal_id,
            actor=actor,
            idempotency_key=str(payload.get("idempotency_key", proposal_id)),
            identity_binding=identity_binding,
            reviewer_principal=reviewer_principal,
        )
    except Exception as error:
        code = getattr(error, "code", None)
        if code is not None:
            self._json(HTTPStatus.CONFLICT, {"error": code})
            return True
        status = HTTPStatus.NOT_FOUND if isinstance(error, NotFoundError) else HTTPStatus.CONFLICT
        self._json(status, {"error": "migration_proposal_approval_failed"})
        return True
    proposal = owner.migration_manager.get_proposal(owner.kernel.subject_id, proposal_id)
    self._json(
        HTTPStatus.ACCEPTED,
        dict(task.__dict__)
        | {
            "approval_count": proposal["approval_count"],
            "required_approvals": proposal["required_approvals"],
            "approval_status": (
                "approved" if task.status == "approved" else "awaiting_second_approval"
            ),
        },
    )
    return True
    return False


def admin_migration_proposals_reject(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    proposal_id = (
        self.path.removeprefix("/api/admin/migration/proposals/").removesuffix("/reject").strip("/")
    )
    payload = self._request_json()
    if payload is None:
        return True
    try:
        rejected = owner.migration_manager.reject(
            proposal_id,
            actor=self._actor(),
            reason=str(payload.get("reason", "rejected by operator")),
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "migration_proposal_not_found"})
        return True
    except ValueError:
        self._json(
            HTTPStatus.CONFLICT,
            {"error": "migration_proposal_rejection_failed"},
        )
        return True
    self._json(HTTPStatus.OK, rejected.__dict__)
    return True
    return False


def admin_migration_tasks_cancel(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    task_id = (
        self.path.removeprefix("/api/admin/migration/tasks/").removesuffix("/cancel").strip("/")
    )
    payload = self._request_json()
    if payload is None:
        return True
    try:
        cancelled = owner.migration_manager.cancel(
            task_id,
            actor=self._actor(),
            reason=str(payload.get("reason", "")),
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "migration_task_not_found"})
        return True
    except ValueError:
        self._json(HTTPStatus.CONFLICT, {"error": "migration_task_cancel_failed"})
        return True
    self._json(HTTPStatus.OK, cancelled.__dict__)
    return True
    return False


def admin_migration_tasks_cutover(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return True
    task_id = (
        self.path.removeprefix("/api/admin/migration/tasks/").removesuffix("/cutover").strip("/")
    )
    payload = self._request_json()
    if payload is None:
        return True
    if not payload:
        try:
            owner.start_migration(task_id)
        except (ValueError, NotFoundError):
            self._json(HTTPStatus.CONFLICT, {"error": "migration_cutover_rejected"})
            return True
        self._json(HTTPStatus.ACCEPTED, {"task_id": task_id, "status": "queued"})
        return True
    proof = payload.get("proof")
    if not isinstance(proof, dict):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "verified_target_restore_and_health_proof_required"},
        )
        return True
    try:
        owner.migration_cutover.prepare(
            task_id,
            actor=self._actor(),
            proof=proof,
        )
        result = owner.migration_cutover.commit(task_id, actor=self._actor())
    except (ValueError, MigrationExecutionError):
        self._json(
            HTTPStatus.CONFLICT,
            {"error": "migration_cutover_rejected"},
        )
        return True
    self._json(HTTPStatus.OK, result)
    return True
    return False


def admin_migration_tasks_rollback(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    task_id = (
        self.path.removeprefix("/api/admin/migration/tasks/").removesuffix("/rollback").strip("/")
    )
    payload = self._request_json()
    if payload is None:
        return True
    try:
        result = owner.migration_cutover.rollback(
            task_id,
            str(payload.get("reason", "operator rollback")),
            actor=self._actor(),
        )
    except ValueError:
        self._json(HTTPStatus.CONFLICT, {"error": "migration_rollback_rejected"})
        return True
    self._json(HTTPStatus.OK, result)
    return True
    return False


def admin_migration_recovery(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    payload = self._request_json()
    if payload is None:
        return True
    try:
        policy = owner.migration_store.read_policy(owner.kernel.subject_id)
        result = owner.migration_recovery.restore_standby(
            RecoveryRequest(
                task_id=str(payload["task_id"]),
                standby_target_id=str(payload["standby_target_id"]),
                verified_backup_id=str(payload["verified_backup_id"]),
                source_failure_evidence=str(payload["source_failure_evidence"]),
                manifest_digest=str(payload["manifest_digest"]),
                restore_report_digest=str(payload["restore_report_digest"]),
                health_report_digest=str(payload["health_report_digest"]),
                target_signature=str(payload["target_signature"]),
            ),
            policy,
        )
    except (KeyError, ValueError):
        self._json(HTTPStatus.CONFLICT, {"error": "migration_recovery_rejected"})
        return True
    self._json(HTTPStatus.ACCEPTED, result)
    return True
    return False
