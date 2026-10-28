"""Read migration HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import SplitResult

from noyra.core.errors import NotFoundError

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def admin_migration_policy(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    policy = owner.migration_store.read_policy(owner.kernel.subject_id)
    projection = dict(policy.__dict__)
    with owner.kernel.database.connection() as connection:
        epoch = connection.execute(
            "SELECT epoch_id,target_id,epoch_number,status,acquired_at,revoked_at "
            "FROM migration_epochs WHERE subject_id=? AND status='active' "
            "ORDER BY epoch_number DESC LIMIT 1",
            (owner.kernel.subject_id,),
        ).fetchone()
    projection["active_epoch"] = None if epoch is None else dict(epoch)
    projection["assessment_status"] = owner.migration_runtime.status
    self._json(
        HTTPStatus.OK,
        projection,
    )
    return True
    return False


def admin_migration_targets(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized("operator"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    with owner.kernel.database.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM migration_targets WHERE subject_id=? ORDER BY created_at DESC LIMIT ?",
            (owner.kernel.subject_id, limit),
        ).fetchall()
    output = []
    for row in rows:
        try:
            owner.migration_targets._assert_row_integrity(row)
        except ValueError:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "migration_target_integrity_unavailable"},
            )
            return True
        item = {
            key: row[key]
            for key in (
                "target_id",
                "subject_id",
                "key_fingerprint",
                "recipient_public_key",
                "recipient_key_fingerprint",
                "enrollment_generation",
                "endpoint",
                "region",
                "provider",
                "release_sha",
                "os_arch",
                "encrypted_volume",
                "status",
                "attested_at",
                "attestation_epoch",
                "created_at",
                "updated_at",
            )
        }
        item["capabilities_json"] = row["capabilities_json"]
        item["capabilities"] = json.loads(item.pop("capabilities_json"))
        output.append(item)
    self._json(HTTPStatus.OK, output)
    return True
    return False


def admin_migration_candidates(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        candidates = owner.migration_runtime.refresh_candidates()
    except Exception:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "migration_discovery_unavailable"},
        )
        return True
    self._json(HTTPStatus.OK, candidates)
    return True
    return False


def admin_migration_proposals(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized("operator"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        items = owner.migration_manager.list_proposals(owner.kernel.subject_id, limit=limit)
    except ValueError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "migration_proposal_integrity_unavailable"},
        )
        return True
    self._json(HTTPStatus.OK, items)
    return True
    return False


def admin_migration_proposals_item(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult
) -> bool:
    if not self._authorized("operator"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    proposal_id = parsed.path.removeprefix("/api/admin/migration/proposals/").strip("/")
    if not proposal_id or "/" in proposal_id:
        self._json(HTTPStatus.NOT_FOUND, {"error": "migration_proposal_not_found"})
        return True
    try:
        proposal = owner.migration_manager.get_proposal(owner.kernel.subject_id, proposal_id)
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "migration_proposal_not_found"})
        return True
    except ValueError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "migration_proposal_integrity_unavailable"},
        )
        return True
    self._json(HTTPStatus.OK, proposal)
    return True
    return False


def admin_migration_tasks(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized("operator"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        tasks = owner.migration_manager.list_tasks(owner.kernel.subject_id, limit=limit)
    except ValueError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "migration_task_integrity_unavailable"},
        )
        return True
    projections = []
    for task in tasks:
        projection = dict(task.__dict__)
        projection["self_attestation_available"] = owner.migration_cutover.has_self_attestation(
            task.task_id
        )
        projections.append(projection)
    self._json(HTTPStatus.OK, projections)
    return True
    return False


def admin_migration_tasks_item(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult
) -> bool:
    if not self._authorized("operator"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    task_path = parsed.path.removeprefix("/api/admin/migration/tasks/").strip("/")
    if task_path.endswith("/self-attestation"):
        task_id = task_path.removesuffix("/self-attestation").strip("/")
        if not task_id or "/" in task_id:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"error": "migration_task_not_found"},
            )
            return True
        try:
            report = owner.migration_cutover.get_self_attestation(
                task_id, subject_id=owner.kernel.subject_id
            )
        except NotFoundError:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"error": "migration_task_not_found"},
            )
            return True
        except ValueError:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "migration_self_attestation_unavailable"},
            )
            return True
        self._json(HTTPStatus.OK, report)
        return True
    task_id = task_path
    if not task_id or "/" in task_id:
        self._json(HTTPStatus.NOT_FOUND, {"error": "migration_task_not_found"})
        return True
    try:
        task = owner.migration_manager.get_task(task_id, subject_id=owner.kernel.subject_id)
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "migration_task_not_found"})
        return True
    except ValueError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "migration_task_integrity_unavailable"},
        )
        return True
    self._json(HTTPStatus.OK, task.__dict__)
    return True
    return False
