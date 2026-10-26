"""Read connections HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import SplitResult

from noyra.core.errors import IntegrityError, NotFoundError
from noyra.research.routing import get_search_routing_mode, list_search_provider_controls

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def common_knowledge_public_key(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._json(
        HTTPStatus.OK,
        {
            "key_id": owner.common_knowledge.key_id,
            "public_key": owner.common_knowledge.public_key,
        },
    )
    return False


def common_knowledge_discovery(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._json(HTTPStatus.OK, owner.common_knowledge.discovery_document())
    return False


def common_knowledge_feed(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]], limit: int
) -> bool:
    raw_cursor = query.get("cursor", ["0"])[0]
    if not raw_cursor.isdigit():
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_cursor"})
        return True
    try:
        document = owner.common_knowledge.feed_document(
            cursor=int(raw_cursor),
            limit=limit,
        )
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_cursor"})
        return True
    self._json(HTTPStatus.OK, document)
    return False


def common_knowledge_public(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    self._json(
        HTTPStatus.OK,
        [
            owner.common_knowledge.export_package(record.package_id)
            for record in owner.common_knowledge.published(limit=limit)
        ],
    )
    return False


def config_search_providers(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        controls = list_search_provider_controls(owner.kernel.database, owner.kernel.subject_id)
        search_records = owner.search_providers.list(owner.kernel.subject_id)
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "search_provider_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(
        HTTPStatus.OK,
        [
            {
                "config_id": record.config_id,
                "provider_type": record.provider_type,
                "label": record.label,
                "key_fingerprint": record.key_fingerprint[:12],
                "rate_limit_per_hour": record.rate_limit_per_hour,
                "priority": record.priority,
                "weight": record.weight,
                "status": record.status,
                "enabled": controls.get(record.config_id, True)
                if record.status == "active"
                else False,
                "created_at": record.created_at,
                "revoked_at": record.revoked_at,
            }
            for record in search_records
        ],
    )
    return False


def config_search_routing(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        mode, updated_at = get_search_routing_mode(owner.kernel.database, owner.kernel.subject_id)
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "search_routing_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(
        HTTPStatus.OK,
        {"mode": mode, "updated_at": updated_at},
    )
    return False


def config_identity_challenges_item(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        result = owner.inbound.identity_challenge(
            owner.kernel.subject_id, parsed.path.rsplit("/", 1)[-1]
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "identity_challenge_not_found"})
        return True
    self._json(HTTPStatus.OK, result)
    return False


def config_inbound_bindings(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        owner.inbound.list_bindings(owner.kernel.subject_id, limit=limit),
    )
    return False


def config_common_knowledge(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        {
            "published": [
                {
                    **owner.common_knowledge.package_metadata(record.package_id),
                    "import_status": "published",
                    "can_revoke": True,
                }
                for record in owner.common_knowledge.published(limit=limit)
            ],
            "review_queue": owner.common_knowledge.review_queue(limit=limit),
            "peers": [record.__dict__ for record in owner.common_knowledge.peers()],
        },
    )
    return False


def config_transports(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        [record.__dict__ for record in owner.transports.list(owner.kernel.subject_id)],
    )
    return False


def config_capabilities(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        [
            {
                **record.__dict__,
                "effective_status": (
                    "blocked_legacy_approval" if record.requires_approval else record.status
                ),
            }
            for record in owner.capabilities.list(owner.kernel.subject_id)
        ],
    )
    return False


def deliveries(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    with owner.kernel.database.connection() as connection:
        delivery_rows = connection.execute(
            "SELECT delivery_id FROM interaction_deliveries WHERE subject_id = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (owner.kernel.subject_id, limit),
        ).fetchmany(limit)
    self._json(
        HTTPStatus.OK,
        [owner.deliveries.get_delivery(str(row["delivery_id"])).__dict__ for row in delivery_rows],
    )
    return False


def deliveries_reconciliations(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult
) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    delivery_id = (
        parsed.path.removeprefix("/api/deliveries/").removesuffix("/reconciliations").strip("/")
    )
    try:
        reconciliation_records = owner.deliveries.reconciliation_history(
            delivery_id,
            subject_id=owner.kernel.subject_id,
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "delivery_not_found"})
        return True
    self._json(
        HTTPStatus.OK,
        [record.__dict__ for record in reconciliation_records],
    )
    return False
