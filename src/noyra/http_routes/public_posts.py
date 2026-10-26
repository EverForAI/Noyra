"""Public posts HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from noyra.core.errors import IntegrityError, NotFoundError
from noyra.interaction import (
    PublicPostCapacityError,
    PublicPostConflictError,
    PublicPostQueueFullError,
    PublicPostRateLimitError,
)
from noyra.interaction.public_archive import PublicationCapacityError, PublicationConflict

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def public_publication(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
        if self.path.endswith("/withdraw"):
            if set(payload) != {"expected_revision"}:
                raise ValueError("invalid withdrawal")
            identifier = self.path.removeprefix("/api/admin/public-publications/").removesuffix(
                "/withdraw"
            )
            owner.public_archive.withdraw(
                owner.kernel.subject_id,
                identifier,
                payload["expected_revision"],
                actor=self._actor(),
            )
            result = {"status": "withdrawn"}
        else:
            result = owner.public_archive.publish(
                owner.kernel.subject_id, payload, actor=self._actor()
            )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "public_publication_not_found"})
        return
    except PublicationConflict:
        self._json(HTTPStatus.CONFLICT, {"error": "public_publication_conflict"})
        return
    except PublicationCapacityError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "public_publication_capacity_full"})
        return
    except IntegrityError:
        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "public_content_unavailable"})
        return
    except (ValueError, TypeError, UnicodeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_public_publication"})
        return
    self._json(HTTPStatus.OK, result)


def configure_public_post_controls(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
    fields = (
        "rate_limit_per_hour",
        "queue_cap",
        "captcha_ttl_seconds",
        "captcha_max_attempts",
        "storage_cap_bytes",
        "captcha_issue_limit_per_hour",
        "captcha_global_rate_per_minute",
    )
    if any(type(payload.get(field)) is not int for field in fields) or not isinstance(
        payload.get("captcha_mode"), str
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_public_post_controls"})
        return
    if any(
        name in payload and type(payload[name]) is not int
        for name in ("max_content_chars", "expected_length_revision")
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_public_post_controls"})
        return
    try:
        controls = owner.public_posts.configure_controls(
            owner.kernel.subject_id,
            rate_limit_per_hour=payload["rate_limit_per_hour"],
            queue_cap=payload["queue_cap"],
            captcha_ttl_seconds=payload["captcha_ttl_seconds"],
            captcha_max_attempts=payload["captcha_max_attempts"],
            captcha_mode=payload["captcha_mode"],
            storage_cap_bytes=payload["storage_cap_bytes"],
            captcha_issue_limit_per_hour=payload["captcha_issue_limit_per_hour"],
            captcha_global_rate_per_minute=payload["captcha_global_rate_per_minute"],
            actor=self._actor(),
            max_content_chars=payload.get("max_content_chars"),
            expected_length_revision=payload.get("expected_length_revision"),
        )
        controls = owner.public_post_controls()
        owner.audit_admin_event(
            "public_post_controls_updated",
            self._actor(),
            {
                key: controls[key]
                for key in (
                    "rate_limit_per_hour",
                    "queue_cap",
                    "captcha_ttl_seconds",
                    "captcha_max_attempts",
                    "captcha_mode",
                    "storage_cap_bytes",
                    "captcha_issue_limit_per_hour",
                    "captcha_global_rate_per_minute",
                    "max_content_chars",
                    "length_revision",
                )
            },
        )
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "public_post_integrity_unavailable"},
            retry_after=60,
        )
        return
    except PublicPostConflictError:
        self._json(HTTPStatus.CONFLICT, {"error": "public_post_controls_conflict"})
        return
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_public_post_controls"})
        return
    self._json(HTTPStatus.OK, controls)


def issue_public_post_captcha(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    """Issue a stateful challenge only to a same-origin JSON POST."""
    fetch_site = self.headers.get("Sec-Fetch-Site", "").strip().casefold()
    origin = self.headers.get("Origin", "").strip()
    host = self.headers.get("Host", "").strip().casefold()
    if fetch_site == "cross-site" or (
        origin
        and (
            urlsplit(origin).scheme not in {"http", "https"}
            or urlsplit(origin).netloc.casefold() != host
        )
    ):
        self._discard_small_request_body()
        self._json(HTTPStatus.FORBIDDEN, {"error": "cross_site_request_forbidden"})
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
        or set(payload) - {"presentation"}
        or not isinstance(payload.get("presentation", "image"), str)
        or payload.get("presentation", "image") != "image"
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_captcha_request"})
        return
    try:
        controls = owner.public_post_controls()
        challenge = owner.public_posts.issue_captcha(
            owner.kernel.subject_id,
            owner.client_ip(str(self.client_address[0]), self.headers.get("X-Forwarded-For")),
            ttl_seconds=controls["captcha_ttl_seconds"],
            max_attempts=controls["captcha_max_attempts"],
            mode=controls["captcha_mode"],
            presentation=payload.get("presentation", "image"),
            issue_limit_per_hour=controls["captcha_issue_limit_per_hour"],
            global_rate_per_minute=controls["captcha_global_rate_per_minute"],
            queue_cap=controls["queue_cap"],
            storage_cap_bytes=controls["storage_cap_bytes"],
        )
    except PublicPostRateLimitError:
        self._json(
            HTTPStatus.TOO_MANY_REQUESTS,
            {"error": "captcha_rate_limited"},
            retry_after=60,
        )
        return
    except PublicPostQueueFullError:
        self._json(
            HTTPStatus.TOO_MANY_REQUESTS,
            {"error": "public_post_queue_full"},
            retry_after=60,
        )
        return
    except PublicPostCapacityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "public_post_capacity_full"},
            retry_after=60,
        )
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "public_post_integrity_unavailable"},
            retry_after=60,
        )
        return
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_captcha_request"})
        return
    self._json(HTTPStatus.OK, challenge)
