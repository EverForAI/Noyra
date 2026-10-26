"""Read public HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import SplitResult, parse_qs

from noyra.core.errors import IntegrityError
from noyra.interaction.public_archive import ARCHIVE_CONTRACT, PublicQueryBudgetExceeded

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def public_item(self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult) -> bool:
    allowed_routes = {
        "overview": None,
        "timeline": "all",
        "diary": "diary",
        "behavior": "behavior",
        "interactions": "interaction",
        "posts": "post",
        "goals": "goal",
        "projects": "project",
        "predictions": "prediction",
        "research": "research",
        "sleep": "sleep",
        "lifecycle": "lifecycle",
        "updates": "update",
    }
    route = parsed.path.removeprefix("/api/public/")
    if route not in allowed_routes:
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        return True
    try:
        params = parse_qs(parsed.query, keep_blank_values=True)
        allowed_params = {
            "limit",
            "cursor",
            "since",
            "until",
            "search",
            "status",
            "kind",
            "id",
        }
        if set(params) - allowed_params or any(len(v) != 1 for v in params.values()):
            raise ValueError("invalid public query")
        if route == "overview":
            if params:
                raise ValueError("overview takes no query")
            result = owner.public_archive.overview(owner.kernel.subject_id)
        else:
            kind = params.get("kind", [allowed_routes[route]])[0]
            if route != "timeline" and kind != allowed_routes[route]:
                raise ValueError("invalid public category")
            result = owner.public_archive.page(
                owner.kernel.subject_id,
                kind=str(kind),
                limit=int(params.get("limit", ["20"])[0]),
                cursor=params.get("cursor", [None])[0],
                since=params.get("since", [None])[0],
                until=params.get("until", [None])[0],
                search=params.get("search", [""])[0],
                status=params.get("status", [""])[0],
                identifier=params.get("id", [None])[0],
            )
    except (ValueError, TypeError, UnicodeError):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_public_query"},
            public_contract=ARCHIVE_CONTRACT,
        )
        return True
    except PublicQueryBudgetExceeded:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "public_query_budget_exceeded"},
            public_contract=ARCHIVE_CONTRACT,
        )
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "public_content_unavailable"},
            public_contract=ARCHIVE_CONTRACT,
        )
        return True
    self._json(HTTPStatus.OK, result, public_contract=ARCHIVE_CONTRACT)
    return False


def state(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    state = owner.projection.state(owner.kernel.subject_id)
    self._json(HTTPStatus.OK, state, public_contract=True)
    return False


def state_details(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    state = owner.projection.private_state(owner.kernel.subject_id)
    if owner.cognition_gateway is not None:
        state["cognitive_resources"] = owner.cognition_gateway.pool_status()
    self._json(HTTPStatus.OK, state)
    return False


def diary(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    self._json(
        HTTPStatus.OK,
        owner.projection.diary(owner.kernel.subject_id, limit=limit),
        public_contract=True,
    )
    return False


def projects(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        owner.projection.projects_view(owner.kernel.subject_id, limit=limit),
    )
    return False


def public_posts(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    try:
        posts = owner.projection.public_posts_view(owner.kernel.subject_id, limit=limit)
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "public_post_integrity_unavailable"},
            retry_after=60,
            public_contract=True,
        )
        return True
    self._json(HTTPStatus.OK, posts, public_contract=True)
    return False
