"""Read exports HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import SplitResult

from noyra.core.errors import NotFoundError

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def config_training_policy(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        owner.training.policy(owner.kernel.subject_id).__dict__,
    )
    return False


def admin_export_jobs(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized("export"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        [
            job.__dict__
            for job in owner.export_jobs.list(
                owner.kernel.subject_id,
                limit=limit,
            )
        ],
    )
    return False


def admin_export_jobs_item(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult
) -> bool:
    if not self._authorized("export"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    suffix = parsed.path.removeprefix("/api/admin/export-jobs/")
    if suffix.endswith("/download"):
        job_id = suffix.removesuffix("/download").strip("/")
        try:
            job, path = owner.export_jobs.download_path(
                job_id,
                subject_id=owner.kernel.subject_id,
            )
        except NotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "export_job_not_found"})
            return True
        except FileNotFoundError:
            self._json(
                HTTPStatus.GONE,
                {"error": "export_artifact_missing"},
            )
            return True
        except RuntimeError:
            self._json(
                HTTPStatus.CONFLICT,
                {"error": "export_not_ready"},
            )
            return True
        self._send_file(
            path,
            filename=job.filename or f"{job_id}.zip",
            digest=job.sha256 or "",
            export_job_id=job_id,
        )
    else:
        try:
            job = owner.export_jobs.get(
                suffix.strip("/"),
                subject_id=owner.kernel.subject_id,
            )
        except NotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "export_job_not_found"})
            return True
        self._json(HTTPStatus.OK, job.__dict__)
    return False


def admin_runtime_export(self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult) -> bool:
    if not owner.allow_mutation(parsed.path):
        self.close_connection = True
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "integrity_quarantine"},
        )
        return True
    self._enqueue_export_job("runtime")
    return False


def admin_training_export(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult
) -> bool:
    if not owner.allow_mutation(parsed.path):
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "integrity_quarantine"},
        )
        return True
    self._enqueue_export_job("training")
    return False
