"""Read cognition HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import SplitResult

from pydantic import SecretStr

from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.usage_projection import UsageProjection, UsageQueryLimit
from noyra.model import OpenAICompatibleSettings
from noyra.model.capabilities import CapabilityProfileStore
from noyra.model.reasoning import reasoning_profile_payload

from .validation import _cognitive_resource_payload

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def admin_usage(self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]]) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        usage_payload = UsageProjection(owner.kernel.database).summary(
            owner.kernel.subject_id,
            window=query.get("window", ["7"])[0],
            offset_minutes=int(query.get("offset_minutes", ["0"])[0]),
            pool=query.get("pool", [None])[0],
            resource=query.get("resource", [None])[0],
        )
    except UsageQueryLimit:
        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "usage_query_limit"})
        return True
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_usage_query"})
        return True
    except sqlite3.OperationalError:
        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "usage_unavailable"})
        return True
    self._json(HTTPStatus.OK, usage_payload)
    return False


def config_model_capabilities(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]]
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    model = query.get("model", [""])[0]
    provider_type = query.get("provider_type", ["openai_compatible"])[0]
    base_url = query.get("base_url", [""])[0]
    if (
        not isinstance(model, str)
        or not 1 <= len(model.strip()) <= 256
        or provider_type != "openai_compatible"
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_capability"})
        return True
    if base_url:
        try:
            OpenAICompatibleSettings(
                base_url=base_url,
                model=model.strip(),
                api_key=SecretStr("capability-probe"),
            )
        except (TypeError, ValueError):
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_model_capability"},
            )
            return True
    stored = None
    if base_url:
        stored = CapabilityProfileStore(owner.kernel.database).get(
            owner.kernel.subject_id,
            provider_type,
            base_url,
            model.strip(),
        )
    profile = (
        stored.public() if stored is not None else reasoning_profile_payload(model, provider_type)
    )
    profile.update(
        {
            "model": model.strip(),
            "provider_type": provider_type,
            "source": profile.get("source", "server_profile"),
            "status": profile.get("status", "unobserved"),
        }
    )
    self._json(HTTPStatus.OK, profile)
    return False


def config_model_resources_item(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    parts = parsed.path.removeprefix("/api/config/model-resources/").split("/")
    if len(parts) == 2 and parts[1] == "keys" and parts[0]:
        try:
            key_rows = owner.cognitive_resources.keys(parts[0], subject_id=owner.kernel.subject_id)
        except NotFoundError:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"error": "model_resource_not_found"},
            )
            return True
        except IntegrityError:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "model_resource_integrity_unavailable"},
                retry_after=60,
            )
            return True
        self._json(
            HTTPStatus.OK,
            [
                {
                    "key_id": row.key_id,
                    "group_id": row.group_id,
                    "key_fingerprint": row.key_fingerprint[:12],
                    "status": row.status,
                    "selection_count": row.selection_count,
                    "consecutive_failures": row.consecutive_failures,
                    "cooldown_until": row.cooldown_until,
                    "last_selected_at": row.last_selected_at,
                    "last_success_at": row.last_success_at,
                    "last_failure_at": row.last_failure_at,
                    "created_at": row.created_at,
                }
                for row in key_rows
            ],
        )
        return True
    # This prefix branch must terminate every unmatched path.
    # Without an explicit response, malformed resource URLs
    # (for example ``/{id}/keys/extra``) fall out of the
    # ``elif`` chain with no HTTP response and leave clients
    # waiting until their request timeout.
    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
    return False


def config_model_resources(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]], limit: int
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        model_resource_records = owner.cognitive_resources.list(
            owner.kernel.subject_id,
            pool=query.get("pool", [None])[0],
            status=query.get("status", [None])[0],
            search=query.get("search", [None])[0],
            limit=limit if query.get("view", [""])[0] in {"management", "usage"} else None,
            offset=int(query.get("offset", ["0"])[0]),
        )
    except (ValueError, TypeError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_query"})
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "model_resource_integrity_unavailable"},
            retry_after=60,
        )
        return True
    model_payloads = [
        _cognitive_resource_payload(group, owner.kernel.database)
        for group in model_resource_records
    ]
    if query.get("view", [""])[0] == "management":
        try:
            for group, model_item in zip(model_resource_records, model_payloads, strict=True):
                model_item.update(owner.cognitive_resources.budget_summary(group))
        except IntegrityError:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "model_resource_integrity_unavailable"},
                retry_after=60,
            )
            return True
    self._json(HTTPStatus.OK, model_payloads)
    return False


def config_embedding_resources(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    runtime_status = owner.embedding_runtime_status()
    effective_id = runtime_status.get("effective_resource_id")
    self._json(
        HTTPStatus.OK,
        [
            {
                **record.__dict__,
                "key_fingerprint": record.key_fingerprint[:12],
                "runtime_effective": (
                    record.status == "active"
                    and record.config_id == effective_id
                    and runtime_status.get("effective") is True
                ),
                "restart_required": (
                    record.status == "active" and record.config_id != effective_id
                ),
            }
            for record in owner.embedding_resources.list(owner.kernel.subject_id)
        ],
    )
    return False
