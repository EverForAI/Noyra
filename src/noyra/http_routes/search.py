"""Search HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import utc_now
from noyra.research import SearchExecutor, SearchProviderInput
from noyra.research.routing import set_search_provider_enabled, set_search_routing_mode

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def configure_search_provider(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
        proposal = SearchProviderInput.model_validate(payload)
        record = owner.search_providers.configure(
            owner.kernel.subject_id,
            proposal,
            actor=self._actor(),
        )
    except (ValueError, TypeError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search_provider"})
        return
    self._json(
        HTTPStatus.CREATED,
        {
            "config_id": record.config_id,
            "provider_type": record.provider_type,
            "label": record.label,
            "status": record.status,
        },
    )


def configure_search_routing(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
    if (
        not isinstance(payload, dict)
        or set(payload) != {"mode"}
        or not isinstance(payload.get("mode"), str)
        or payload["mode"] not in {"model_first", "api_first", "auto"}
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search_routing"})
        return
    mode = str(payload["mode"])
    now = set_search_routing_mode(owner.kernel.database, owner.kernel.subject_id, mode)
    owner.audit_admin_event("search_routing_updated", self._actor(), {"mode": mode})
    self._json(HTTPStatus.OK, {"mode": mode, "updated_at": now})


def set_search_provider_routing(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
    if (
        not isinstance(payload, dict)
        or set(payload) != {"priority", "weight", "reason"}
        or isinstance(payload.get("priority"), bool)
        or not isinstance(payload.get("priority"), int)
        or isinstance(payload.get("weight"), bool)
        or not isinstance(payload.get("weight"), int)
        or not isinstance(payload.get("reason"), str)
        or not payload["reason"].strip()
        or len(payload["reason"]) > 500
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search_provider_routing"})
        return
    config_id = self.path.removeprefix("/api/config/search-providers/").removesuffix("/routing")
    if not config_id or "/" in config_id:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search_provider_routing"})
        return
    try:
        record = owner.search_providers.set_routing(
            config_id,
            owner.kernel.subject_id,
            priority=payload["priority"],
            weight=payload["weight"],
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "search_provider_not_found"})
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "search_provider_integrity_unavailable"},
            retry_after=60,
        )
        return
    except (ValueError, TypeError, PermissionError):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_search_provider_routing"},
        )
        return
    owner.audit_admin_event(
        "search_provider_routing_updated",
        self._actor(),
        {
            "config_id": record.config_id,
            "priority": record.priority,
            "weight": record.weight,
            "reason": payload["reason"].strip(),
        },
    )
    self._json(
        HTTPStatus.OK,
        {
            "config_id": record.config_id,
            "priority": record.priority,
            "weight": record.weight,
            "updated_at": utc_now(),
        },
    )


def handle_set_search_provider_enabled(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
    if not isinstance(payload, dict) or set(payload) != {"reason"}:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search_provider"})
        return
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search_provider"})
        return
    prefix = "/api/config/search-providers/"
    suffix = "/enable" if self.path.endswith("/enable") else "/disable"
    config_id = self.path.removeprefix(prefix).removesuffix(suffix)
    try:
        now = set_search_provider_enabled(
            owner.kernel.database,
            config_id,
            owner.kernel.subject_id,
            suffix == "/enable",
        )
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search_provider"})
        return
    owner.audit_admin_event(
        "search_provider_enabled" if suffix == "/enable" else "search_provider_disabled",
        self._actor(),
        {"config_id": config_id, "reason": reason},
    )
    self._json(
        HTTPStatus.OK,
        {"config_id": config_id, "enabled": suffix == "/enable", "updated_at": now},
    )


def test_unstored_search_provider(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
    if (
        not isinstance(payload, dict)
        or set(payload) != {"provider_type", "api_key"}
        or not isinstance(payload.get("provider_type"), str)
        or not isinstance(payload.get("api_key"), str)
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search_provider_test"})
        return
    provider_type = payload["provider_type"]
    api_key = payload["api_key"]
    try:
        if not api_key.strip() or len(api_key) > 4_096:
            raise ValueError("invalid key")

        async def _test() -> dict[str, Any]:
            executor = SearchExecutor(owner.kernel.database, owner.search_providers)
            try:
                return await executor.test_unstored(provider_type, SecretStr(api_key))
            finally:
                await executor.aclose()

        result = asyncio.run(_test())
    except ValueError:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_search_provider_test"},
        )
        return
    except Exception as error:
        safe_error = (
            "search_provider_test_unknown"
            if type(error).__name__ == "SearchOutcomeUnknownError"
            else "search_provider_test_failed"
        )
        owner.audit_admin_event(
            "search_provider_test_failed",
            self._actor(),
            {"provider_type": provider_type, "error": safe_error},
        )
        self._json(HTTPStatus.BAD_GATEWAY, {"error": safe_error})
        return
    owner.audit_admin_event(
        "search_provider_test_succeeded",
        self._actor(),
        {
            "provider_type": provider_type,
            "result_count": result["result_count"],
        },
    )
    self._json(HTTPStatus.OK, result)


def revoke_search_provider(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
    config_id = self.path.removeprefix("/api/config/search-providers/").removesuffix("/revoke")
    reason = payload.get("reason")
    try:
        if not isinstance(reason, str):
            raise TypeError("reason must be text")
        record = owner.search_providers.revoke(
            config_id,
            reason=reason,
            actor=self._actor(),
            subject_id=owner.kernel.subject_id,
        )
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search_provider"})
        return
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "search_provider_not_found"})
        return
    self._json(HTTPStatus.OK, {"config_id": record.config_id, "status": record.status})
