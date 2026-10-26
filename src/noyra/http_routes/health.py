"""Health HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import SplitResult, urlsplit

from noyra.core.archive import S3ArchiveProvider
from noyra.core.errors import IntegrityError
from noyra.core.types import utc_now

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def health_live(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._json(HTTPStatus.OK, {"status": "ok", "service": "noyra"})
    return True
    return False


def health_ready_admin_readiness(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult
) -> bool:
    detailed = parsed.path != "/health/ready"
    if detailed and not self._authorized("operator"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        lifecycle = owner.kernel.lifecycle.current().state
        at_rest = None if owner.at_rest is None else owner.at_rest.request_health()
        at_rest_ready = not bool(
            at_rest is not None and at_rest.get("enforced") and not at_rest.get("ready")
        )
        quarantine = owner.quarantine_checker
        integrity_ready = not bool(quarantine()) if callable(quarantine) else True
        cloud_ready = not (
            owner.cloud_archive_status["configured"] and not owner.cloud_archive_status["ready"]
        )
        storage, storage_checked_at = owner._current_storage()
        storage_runtime_ready = storage.runtime_ready
        storage_backup_ready = storage.backup_ready
        ready = (
            storage_runtime_ready
            and (owner.settings.storage_profile != "recovery_capable" or storage_backup_ready)
            and at_rest_ready
            and integrity_ready
            and cloud_ready
        )
        checked_at = utc_now()
        components = {
            "storage": {
                "status": "ok" if storage_runtime_ready else "degraded",
                "reason_code": (
                    "ready"
                    if storage_runtime_ready
                    else storage.reason_code or "storage_unavailable"
                ),
                "since": None,
                "checked_at": storage_checked_at,
            },
            "backup": {
                "status": "ok" if storage_backup_ready else "degraded",
                "reason_code": (
                    "ready"
                    if storage_backup_ready
                    else storage.backup_reason_code or "backup_unavailable"
                ),
                "since": None,
                "checked_at": storage_checked_at,
            },
            "integrity": {
                "status": "ok" if integrity_ready else "degraded",
                "reason_code": ("ready" if integrity_ready else "integrity_quarantine"),
                "since": None,
                "checked_at": checked_at,
            },
            "cloud_archive": {
                "status": "ok" if cloud_ready else "degraded",
                "reason_code": ("ready" if cloud_ready else "cloud_archive_unavailable"),
                "since": None,
                "checked_at": checked_at,
            },
        }
        payload = {
            "status": "ok" if ready else "degraded",
            "service": "noyra",
            "subject_id": owner.kernel.subject_id,
            "migration_target_id": owner.kernel.migration_target_id,
            "lifecycle": lifecycle,
            "storage": storage.public(),
            "at_rest": at_rest,
            "cloud_archive": owner.cloud_archive_status,
            "components": components,
        }
        if not detailed:
            payload = {"status": payload["status"], "service": "noyra"}
        self._json(
            HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
            payload,
        )
    except Exception:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"status": "degraded", "service": "noyra"},
        )
    return True
    return False


def health_cached(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    cached_health = owner._cached_health()
    if cached_health is not None:
        cached_status, cached_payload = cached_health
        self._json(cached_status, self._health_projection(cached_payload))
        return True
    return False


def health_status(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    provider = owner.cloud_archive_provider
    if isinstance(provider, S3ArchiveProvider):
        readiness = provider.readiness()
        owner.cloud_archive_status.update(
            {key: readiness[key] for key in ("ready", "last_checked_at", "error", "provider_id")}
        )
    state = owner.projection.state(owner.kernel.subject_id)
    with owner.kernel.database.connection() as connection:
        loop_state = connection.execute(
            "SELECT circuit_status, consecutive_failures, last_tick_started_at, "
            "last_successful_tick_at, last_error_type FROM autonomy_loop_state "
            "WHERE subject_id = ?",
            (owner.kernel.subject_id,),
        ).fetchone()
    integrity = None if owner.integrity is None else owner.integrity.summary()
    integrity_degraded = (
        integrity is not None
        and integrity["policy_mode"] != "off"
        and integrity["status"] in {"degraded", "corrupt", "incomplete"}
    )
    secret_cleanup_domains = {
        "transport": owner.transports.secret_cleanup.health(owner.kernel.subject_id, "transport"),
        "search": owner.search_providers.secret_cleanup.health(owner.kernel.subject_id, "search"),
        "cognitive": owner.cognitive_resources.secret_cleanup.health(
            owner.kernel.subject_id, "cognitive"
        ),
        "embedding": owner.embedding_resources.cleanup_health(owner.kernel.subject_id),
    }
    secret_cleanup_pending = sum(
        cast(int, domain["pending"]) for domain in secret_cleanup_domains.values()
    )
    secret_cleanup = {
        "status": "degraded" if secret_cleanup_pending else "ok",
        "pending": secret_cleanup_pending,
        "domains": secret_cleanup_domains,
    }
    embedding_secret_cleanup = secret_cleanup_domains["embedding"]
    at_rest = None if owner.at_rest is None else owner.at_rest.health()
    at_rest_degraded = bool(at_rest is not None and at_rest["enforced"] and not at_rest["ready"])
    operator_health = (
        {"status": "unavailable"}
        if owner.operator_controls is None
        else owner.operator_controls.health()
    )
    operator_health_degraded = operator_health.get("status") == "degraded"
    wallet_acquisition_health = owner.wallet_acquisition_health()
    wallet_acquisition_degraded = wallet_acquisition_health.get("status") == "degraded"
    degraded = (
        (loop_state is not None and loop_state["circuit_status"] == "open")
        or integrity_degraded
        or at_rest_degraded
        or operator_health_degraded
        or wallet_acquisition_degraded
        or (owner.cloud_archive_status["configured"] and not owner.cloud_archive_status["ready"])
        or secret_cleanup_pending != 0
    )
    health_status = HTTPStatus.SERVICE_UNAVAILABLE if degraded else HTTPStatus.OK
    loop_open = loop_state is not None and loop_state["circuit_status"] == "open"
    health_payload = {
        "status": "degraded" if degraded else "ok",
        "lifecycle": state["lifecycle"],
        "loop": None if loop_state is None else dict(loop_state),
        "integrity": integrity,
        "at_rest": at_rest,
        "cloud_archive": owner.cloud_archive_status,
        "secret_cleanup": secret_cleanup,
        "embedding_secret_cleanup": embedding_secret_cleanup,
        "operator_health": operator_health,
        "wallet_acquisition": wallet_acquisition_health,
        "health": operator_health,
        "components": {
            "http": {
                "status": "ok",
                "reason_code": "http_thread_running",
                "since": None,
                "checked_at": utc_now(),
            },
            "autonomy": {
                "status": "degraded" if loop_open else "ok",
                "reason_code": "loop_circuit_open" if loop_open else "loop_running",
                "since": None,
                "checked_at": utc_now(),
            },
            "provider": {
                "status": "ok",
                "reason_code": "provider_health_isolated",
                "since": None,
                "checked_at": utc_now(),
            },
            "reflection": {
                "status": "ok",
                "reason_code": "reflection_idle",
                "since": None,
                "checked_at": utc_now(),
            },
        },
    }
    owner._store_health(health_status, health_payload)
    self._json(health_status, self._health_projection(health_payload))
    return False


def admin_health(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    if owner.operator_controls is None:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "operator_controls_unavailable"},
        )
        return True
    self._json(HTTPStatus.OK, owner.operator_controls.health())
    return False


def admin_provider_health(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]]
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    kind = query.get("kind", ["model"])[0]
    if kind not in {"model", "search"}:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_provider_kind"})
        return True
    try:
        health_offset = int(query.get("offset", ["0"])[0])
        if not 0 <= health_offset <= 1_000_000:
            raise ValueError("invalid offset")
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_provider_query"})
        return True
    try:
        providers = owner.provider_health.list_projection(owner.kernel.subject_id, kind)
        metadata: dict[str, dict[str, Any]] = {}
        if kind == "model":
            for group in owner.cognitive_resources.list(owner.kernel.subject_id):
                keys = owner.cognitive_resources.keys(
                    group.group_id, subject_id=owner.kernel.subject_id, limit=1000
                )
                available = sum(
                    key.status == "active"
                    or (
                        key.status == "cooldown"
                        and key.cooldown_until is not None
                        and key.cooldown_until <= utc_now()
                    )
                    for key in keys
                )
                metadata[group.group_id] = {
                    "label": group.label,
                    "pool": group.pool,
                    "model": group.model,
                    "host": urlsplit(group.base_url).hostname,
                    "resource_status": group.status,
                    "key_count": len(keys),
                    "available_key_count": available,
                    "resource_health": ("healthy" if available else "cooldown"),
                }
        else:
            for provider in owner.search_providers.list(owner.kernel.subject_id):
                metadata[provider.config_id] = {
                    "label": provider.label,
                    "pool": "search",
                    "model": provider.provider_type,
                    "host": None,
                    "resource_status": provider.status,
                }
        observed_ids = {item["provider_id"] for item in providers}
        providers.extend(
            {"provider_id": identifier, "state": "no_data", "attempt_count": 0}
            for identifier in metadata
            if identifier not in observed_ids
        )
        for item in providers:
            item.update(
                metadata.get(
                    item["provider_id"],
                    {
                        "label": "历史资源",
                        "resource_status": "historical",
                        "pool": kind,
                        "model": None,
                        "host": None,
                    },
                )
            )
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "provider_health_integrity_unavailable"},
            retry_after=60,
        )
        return True
    except (ValueError, sqlite3.OperationalError):
        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "provider_health_unavailable"})
        return True
    self._json(
        HTTPStatus.OK,
        {
            "provider_kind": kind,
            "providers": providers[health_offset : health_offset + 50],
            "total": len(providers),
            "page_size": 50,
        },
    )
    return False
