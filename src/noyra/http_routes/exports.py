"""Exports HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, cast

from noyra.core.admission import OperationInvalidated, bind_lease, current_lease
from noyra.core.errors import NotFoundError, RuntimeOwnershipError, TrainingPolicyConflictError
from noyra.core.export_jobs import ExportKind

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def update_training_policy(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
    allowed = {
        "record_enabled",
        "export_enabled",
        "include_private_psychology",
        "include_conversations",
        "include_model_io",
        "include_external_actions",
        "include_workspace",
    }
    expected_version = payload.get("expected_version")
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "expected_training_policy_version_required"},
        )
        return
    changes = {key: payload[key] for key in allowed if key in payload}
    try:
        record = owner.training.update_policy(
            owner.kernel.subject_id,
            actor="operator",
            reason="management_api_update",
            expected_version=expected_version,
            **changes,
        )
    except TrainingPolicyConflictError:
        self._json(
            HTTPStatus.CONFLICT,
            {
                "error": "training_policy_version_conflict",
                "current": owner.training.policy(owner.kernel.subject_id).__dict__,
            },
        )
        return
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_training_policy"})
        return
    self._json(HTTPStatus.OK, record.__dict__)


def create_export_job(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized("export"):
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
    export_kind = payload.get("kind")
    if not isinstance(export_kind, str):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_export_kind"})
        return
    if export_kind not in {"runtime", "training"}:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_export_kind"})
        return
    self._enqueue_export_job(export_kind)


def enqueue_export_job(self: RequestHandler, owner: NoyraHTTPServer, export_kind: str) -> None:
    """Create an export job without doing export work inline.

    This is shared by the explicit POST job API and the legacy
    synchronous GET routes.  Keeping all policy/queue checks in
    one path prevents the compatibility shims from bypassing
    authentication or training consent.
    """
    if current_lease() is None:
        try:
            lease = owner.admission.begin("http_export")
        except (OperationInvalidated, RuntimeOwnershipError):
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "runtime_admission_unavailable"},
            )
            return
        try:
            with bind_lease(lease):
                self._enqueue_export_job(export_kind)
        finally:
            owner.admission.finish(lease)
        return
    if not self._authorized("export"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if export_kind == "runtime" and not owner.settings.developer_log_export_enabled:
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        return
    if export_kind == "training":
        try:
            if not owner.training.policy(owner.kernel.subject_id).export_enabled:
                self._json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "training_export_disabled"},
                )
                return
        except NotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "subject_not_found"})
            return
    try:
        job = owner.export_jobs.create(
            owner.kernel.subject_id,
            cast(ExportKind, export_kind),
        )
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_export_kind"})
        return
    except RuntimeError:
        self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "export_queue_full"})
        return
    self._json(HTTPStatus.ACCEPTED, job.__dict__)


def cancel_export_job(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized("export"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    job_id = self.path.removeprefix("/api/admin/export-jobs/").removesuffix("/cancel")
    if not job_id or "/" in job_id:
        self._discard_small_request_body()
        self._json(HTTPStatus.NOT_FOUND, {"error": "export_job_not_found"})
        return
    self._discard_small_request_body()
    try:
        job = owner.export_jobs.cancel(job_id, owner.kernel.subject_id)
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "export_job_not_found"})
        return
    except RuntimeError:
        self._json(HTTPStatus.CONFLICT, {"error": "export_not_cancellable"})
        return
    self._json(HTTPStatus.OK, job.__dict__)
