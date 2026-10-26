"""Post public HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from noyra.core.errors import IntegrityError, NotFoundError
from noyra.interaction import (
    PublicPostCapacityError,
    PublicPostCaptchaError,
    PublicPostConflictError,
    PublicPostInput,
    PublicPostLengthError,
    PublicPostQueueFullError,
    PublicPostRateLimitError,
)

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


LOGGER = logging.getLogger("noyra.service")


def public_posts_captcha(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._issue_public_post_captcha()
    return True
    return False


def public_posts(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return True
    payload = self._request_json()
    if payload is None:
        return True
    if not isinstance(payload, dict):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_public_post"},
        )
        return True
    try:
        controls = owner.public_post_controls()
        idempotency_key = payload.get("idempotency_key")
        if idempotency_key is not None and not isinstance(idempotency_key, str):
            raise TypeError("idempotency_key must be text")
        post_input = PublicPostInput.model_validate(
            {
                key: value
                for key, value in payload.items()
                if key not in {"idempotency_key", "captcha_id", "captcha_answer"}
            }
        )
        post_record = owner.public_posts.create(
            owner.kernel.subject_id,
            post_input,
            idempotency_key=idempotency_key,
            client_ip=owner.client_ip(
                str(self.client_address[0]), self.headers.get("X-Forwarded-For")
            ),
            captcha_id=payload.get("captcha_id"),
            captcha_answer=payload.get("captcha_answer"),
            rate_limit_per_hour=controls["rate_limit_per_hour"],
            queue_cap=controls["queue_cap"],
            storage_cap_bytes=controls["storage_cap_bytes"],
        )
    except PublicPostLengthError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "public_post_too_long"})
        return True
    except PublicPostCaptchaError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_captcha"})
        return True
    except PublicPostRateLimitError:
        self._json(
            HTTPStatus.TOO_MANY_REQUESTS,
            {"error": "public_post_rate_limited"},
            retry_after=3600,
        )
        return True
    except PublicPostQueueFullError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "public_post_queue_full"},
            retry_after=60,
        )
        return True
    except PublicPostCapacityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "public_post_capacity_full"},
            retry_after=60,
        )
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "public_post_integrity_unavailable"},
            retry_after=60,
        )
        return True
    except (ValueError, TypeError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_public_post"})
        return True
    self._json(
        HTTPStatus.CREATED,
        {"post_id": post_record.post_id, "status": post_record.status},
    )
    return True
    return False


def admin_public_posts_item(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return True
    payload = self._request_json()
    if payload is None:
        return True
    if not isinstance(payload, dict):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_public_post_moderation"},
        )
        return True
    post_id, operation = self.path.removeprefix("/api/admin/public-posts/").rsplit("/", 1)
    try:
        reason = payload.get("reason")
        expected_status = payload.get("expected_status")
        idempotency_key = payload.get("idempotency_key")
        if (
            not isinstance(reason, str)
            or (expected_status is not None and not isinstance(expected_status, str))
            or (idempotency_key is not None and not isinstance(idempotency_key, str))
        ):
            raise TypeError("moderation reason must be text")
        actor = self._actor()
        record = owner.public_posts.moderate(
            post_id,
            subject_id=owner.kernel.subject_id,
            status={
                "publish": "published",
                "reject": "rejected",
                "archive": "archived",
            }[operation],
            actor=actor,
            reason=reason,
            expected_status=expected_status,
            idempotency_key=idempotency_key,
        )
        try:
            owner.audit_admin_event(
                "public_post_moderated",
                actor,
                {
                    "post_id": post_id,
                    "operation": operation,
                    "reason": payload.get("reason"),
                    "status": record.status,
                },
            )
        except Exception:
            LOGGER.exception("public post moderation audit failed")
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "public_post_not_found"})
        return True
    except PublicPostConflictError:
        self._json(
            HTTPStatus.CONFLICT,
            {"error": "public_post_moderation_conflict"},
        )
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "public_post_integrity_unavailable"},
            retry_after=60,
        )
        return True
    except (ValueError, TypeError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_public_post_moderation"})
        return True
    self._json(HTTPStatus.OK, record.__dict__)
    return True
    return False
