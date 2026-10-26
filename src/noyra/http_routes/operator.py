"""Operator HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from noyra.capability import CapabilityGrant
from noyra.core.errors import NotFoundError
from noyra.core.operator_controls import OperatorControlConflict, OperatorControlNotFound
from noyra.core.retention import RetentionPolicyConflict
from noyra.core.upgrade import UpgradeError

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def lifecycle_control(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    controls = owner.operator_controls
    if controls is None:
        self._discard_small_request_body()
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "operator_controls_unavailable"},
        )
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    if set(payload) != {"reason"} or not isinstance(payload.get("reason"), str):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_lifecycle_control"},
        )
        return
    operation = self.path.rsplit("/", 1)[-1]
    try:
        mutation = {
            "pause": controls.pause,
            "resume": controls.resume,
            "reset": controls.reset,
            "wake": controls.wake,
        }[operation](
            actor=self._actor(),
            reason=str(payload["reason"]),
        )
    except OperatorControlConflict as error:
        self._json(self._operator_error_status(error), {"error": error.code})
        return
    self._json(HTTPStatus.OK, mutation.public())


def run_retention_batch(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    if set(payload) - {"reason", "batch_size"} or not isinstance(payload.get("reason"), str):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_retention_run"})
        return
    batch_size = payload.get("batch_size")
    if batch_size is not None and (type(batch_size) is not int or not 1 <= batch_size <= 500):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_retention_run"})
        return
    result = owner.retention.run_batch(owner.kernel.subject_id, batch_size=batch_size)
    owner.audit_admin_event(
        "retention_batch_run",
        self._actor(),
        {
            "reason": payload["reason"],
            "deleted_by_table": result["deleted_by_table"],
            "protected_rows": result["protected_rows"],
        },
    )
    self._json(HTTPStatus.OK, result)


def configure_retention(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    try:
        if (
            set(payload)
            - {
                "settings",
                "expected_revision",
                "backup",
                "backup_expected_revision",
            }
            or "settings" not in payload
            or "expected_revision" not in payload
            or not isinstance(payload["settings"], dict)
            or ("backup" in payload and not isinstance(payload["backup"], dict))
        ):
            raise ValueError("invalid fields")
        result = owner.retention.configure(
            owner.kernel.subject_id,
            payload["settings"],
            expected_revision=payload["expected_revision"],
            actor=self._actor(),
            backup_settings=payload.get("backup"),
            backup_expected_revision=payload.get("backup_expected_revision"),
        )
    except RetentionPolicyConflict:
        self._json(HTTPStatus.CONFLICT, {"error": "retention_policy_conflict"})
        return
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_retention_settings"})
        return
    owner.audit_admin_event("retention_policy_updated", self._actor(), result)
    self._json(HTTPStatus.OK, result)


def reconcile_action(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    controls = owner.operator_controls
    if controls is None:
        self._discard_small_request_body()
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "operator_controls_unavailable"},
        )
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    allowed = {"reason", "outcome", "status", "result", "evidence"}
    if not set(payload) <= allowed or not isinstance(payload.get("reason"), str):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_action_reconciliation"},
        )
        return
    outcome = payload.get("outcome", payload.get("status"))
    if "outcome" in payload and "status" in payload and payload["outcome"] != payload["status"]:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_action_reconciliation"},
        )
        return
    result = payload.get("result", payload.get("evidence", {}))
    action_id = self.path.removeprefix("/api/admin/actions/").removesuffix("/reconcile")
    if not action_id or "/" in action_id or not isinstance(outcome, str):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_action_reconciliation"},
        )
        return
    try:
        record = controls.reconcile_action(
            action_id,
            actor=self._actor(),
            reason=str(payload["reason"]),
            outcome=outcome,
            result=result,
        )
    except OperatorControlNotFound:
        self._json(HTTPStatus.NOT_FOUND, {"error": "action_not_found"})
        return
    except OperatorControlConflict as error:
        self._json(self._operator_error_status(error), {"error": error.code})
        return
    self._json(HTTPStatus.OK, record)


def reconcile_model_call(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    controls = owner.operator_controls
    if controls is None:
        self._discard_small_request_body()
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "operator_controls_unavailable"},
        )
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    allowed = {
        "reason",
        "outcome",
        "status",
        "response",
        "evidence",
        "resolution_evidence",
        "accept_duplicate_risk",
    }
    outcome = payload.get("outcome", payload.get("status"))
    response = payload.get("response", payload.get("evidence"))
    resolution_evidence = payload.get("resolution_evidence")
    accept_duplicate_risk = payload.get("accept_duplicate_risk", False)
    if (
        not set(payload) <= allowed
        or not isinstance(payload.get("reason"), str)
        or not isinstance(outcome, str)
        or (resolution_evidence is not None and not isinstance(resolution_evidence, str))
        or not isinstance(accept_duplicate_risk, bool)
        or (
            "outcome" in payload and "status" in payload and payload["outcome"] != payload["status"]
        )
    ):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_model_call_reconciliation"},
        )
        return
    call_id = self.path.removeprefix("/api/admin/model-calls/").removesuffix("/reconcile")
    if not call_id or "/" in call_id:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_model_call_reconciliation"},
        )
        return
    try:
        record = controls.reconcile_model_call(
            call_id,
            actor=self._actor(),
            reason=str(payload["reason"]),
            outcome=outcome,
            response=response,
            resolution_evidence=resolution_evidence,
            accept_duplicate_risk=accept_duplicate_risk,
        )
    except OperatorControlNotFound:
        self._json(HTTPStatus.NOT_FOUND, {"error": "model_call_not_found"})
        return
    except OperatorControlConflict as error:
        self._json(self._operator_error_status(error), {"error": error.code})
        return
    self._json(HTTPStatus.OK, record)


def grant_capability(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    try:
        record = owner.capabilities.grant(
            owner.kernel.subject_id,
            CapabilityGrant.model_validate(payload),
            actor=self._actor(),
        )
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_capability"})
        return
    self._json(HTTPStatus.CREATED, record.__dict__)


def revoke_capability(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    if not isinstance(payload, dict):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_capability"})
        return
    reason = payload.get("reason")
    grant_id = self.path.removeprefix("/api/config/capabilities/").removesuffix("/revoke")
    if not isinstance(reason, str) or not reason.strip():
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_capability"})
        return
    try:
        record = owner.capabilities.revoke(
            grant_id,
            reason=reason,
            actor=self._actor(),
            subject_id=owner.kernel.subject_id,
        )
    except (ValueError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_capability"})
        return
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "capability_not_found"})
        return
    self._json(HTTPStatus.OK, record.__dict__)


def start_upgrade(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    if set(payload) - {"target_sha", "reason", "idempotency_key"}:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "upgrade_target_invalid"})
        return
    reason = payload.get("reason")
    idempotency_key = payload.get("idempotency_key")
    target_sha = payload.get("target_sha")
    if (
        not isinstance(reason, str)
        or not isinstance(idempotency_key, str)
        or (target_sha is not None and not isinstance(target_sha, str))
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "upgrade_target_invalid"})
        return
    try:
        started = owner.upgrade_manager.start(
            target_sha=target_sha,
            reason=reason,
            idempotency_key=idempotency_key,
        )
    except UpgradeError as error:
        status = {
            "upgrade_target_invalid": HTTPStatus.BAD_REQUEST,
            "upgrade_source_dirty": HTTPStatus.CONFLICT,
            "upgrade_in_progress": HTTPStatus.CONFLICT,
            "upgrade_unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
            "storage_unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
            "upgrade_start_failed": HTTPStatus.SERVICE_UNAVAILABLE,
            "upgrade_install_failed": HTTPStatus.CONFLICT,
            "upgrade_interrupted": HTTPStatus.CONFLICT,
        }.get(error.code, HTTPStatus.SERVICE_UNAVAILABLE)
        owner.audit_admin_event(
            "admin_upgrade_rejected",
            self._actor(),
            {"error_code": error.code},
        )
        self._json(
            status,
            {
                "error": error.code,
                "recommendation": owner.upgrade_manager.recommendation(error.code),
            },
        )
        return
    owner.audit_admin_event(
        "admin_upgrade_started",
        self._actor(),
        {
            "task_id": started["task_id"],
            "target_short_sha": str(started["target_sha"])[:12],
            "reason": reason.strip()[:256],
        },
    )
    self._json(HTTPStatus.ACCEPTED, started)
