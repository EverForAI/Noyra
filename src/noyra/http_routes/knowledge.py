"""Knowledge HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from noyra.core.errors import IntegrityError, NotFoundError
from noyra.knowledge import CommonKnowledgePeerInput, CommonKnowledgeSyncError

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def trust_common_knowledge_key(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    payload = self._request_json()
    if payload is None:
        return
    public_key = payload.get("public_key")
    label = payload.get("label")
    if not isinstance(public_key, str) or not isinstance(label, str):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_trusted_key"})
        return
    try:
        key_id = owner.common_knowledge.trust_key(
            public_key,
            label=label,
            actor=self._actor(),
        )
    except (ValueError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_trusted_key"})
        return
    self._json(HTTPStatus.CREATED, {"key_id": key_id})


def import_common_knowledge(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    payload = self._request_json()
    if payload is None:
        return
    try:
        record = owner.common_knowledge.import_package(payload)
    except PermissionError:
        self._json(HTTPStatus.FORBIDDEN, {"error": "publisher_not_trusted"})
        return
    except (ValueError, TypeError, IntegrityError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_knowledge_package"})
        return
    self._json(
        HTTPStatus.CREATED,
        {"package_id": record.package_id, "status": "quarantined"},
    )


def register_common_knowledge_peer(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    payload = self._request_json()
    if payload is None:
        return
    try:
        record = owner.common_knowledge.register_peer(
            CommonKnowledgePeerInput.model_validate(payload),
            actor=self._actor(),
        )
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_knowledge_peer"})
        return
    self._json(HTTPStatus.CREATED, record.__dict__)


def sync_common_knowledge_peer(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    self._discard_small_request_body()
    peer_id = self.path.removeprefix("/api/config/common-knowledge/peers/").removesuffix("/sync")
    if not peer_id or "/" in peer_id:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_knowledge_peer"})
        return
    try:
        result = owner.common_knowledge.sync_peer(peer_id)
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "knowledge_peer_not_found"})
        return
    except CommonKnowledgeSyncError as error:
        self._json(HTTPStatus.BAD_GATEWAY, {"error": error.code})
        return
    except PermissionError:
        self._json(HTTPStatus.CONFLICT, {"error": "knowledge_peer_disabled"})
        return
    self._json(HTTPStatus.OK, result.__dict__)


def revoke_common_knowledge(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
    reason = payload.get("reason")
    package_id = self.path.removeprefix("/api/config/common-knowledge/").removesuffix("/revoke")
    if not isinstance(reason, str) or not package_id or "/" in package_id:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_knowledge_revoke"})
        return
    try:
        owner.common_knowledge.revoke(
            package_id,
            reason=reason,
            actor=self._actor(),
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "knowledge_package_not_found"})
        return
    except PermissionError:
        self._json(HTTPStatus.FORBIDDEN, {"error": "knowledge_revoke_forbidden"})
        return
    except ValueError:
        self._json(HTTPStatus.CONFLICT, {"error": "knowledge_already_revoked"})
        return
    self._json(HTTPStatus.OK, {"package_id": package_id, "status": "revoked"})
