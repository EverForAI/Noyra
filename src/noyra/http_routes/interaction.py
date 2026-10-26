"""Interaction HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus
from typing import TYPE_CHECKING, Literal, cast

from noyra.core.errors import InvalidTransitionError, NotFoundError
from noyra.interaction import TransportInput
from noyra.interaction.conversations import ConversationConflict

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def configure_conversation(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
        if self.path == "/api/admin/conversations":
            if set(payload) != {"title"}:
                raise ValueError("invalid fields")
            result = owner.conversations.create(owner.kernel.subject_id, payload["title"])
        else:
            if len(self.path.split("/")) != 5 or set(payload) - {
                "title",
                "pinned",
                "archived",
                "expected_revision",
            }:
                raise ValueError("invalid fields")
            revision = payload.get("expected_revision")
            if type(revision) is not int:
                raise ValueError("invalid revision")
            result = owner.conversations.update(
                owner.kernel.subject_id,
                self.path.split("/")[4],
                expected_revision=revision,
                title=payload.get("title"),
                pinned=payload.get("pinned"),
                archived=payload.get("archived"),
            )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "conversation_not_found"})
        return
    except ConversationConflict:
        self._json(HTTPStatus.CONFLICT, {"error": "conversation_conflict"})
        return
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_conversation"})
        return
    self._json(HTTPStatus.OK, result)


def configure_transport(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
        record = owner.transports.configure(
            owner.kernel.subject_id,
            TransportInput.model_validate(payload),
            actor=self._actor(),
        )
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_transport"})
        return
    self._json(
        HTTPStatus.CREATED,
        {
            "transport_id": record.transport_id,
            "channel": record.channel,
            "label": record.label,
            "status": record.status,
        },
    )


def change_transport_status(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
    parts = self.path.removeprefix("/api/config/transports/").split("/")
    reason = payload.get("reason")
    if len(parts) != 2 or not isinstance(reason, str):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_transport"})
        return
    transport_id, operation = parts
    try:
        if operation == "enable":
            record = owner.transports.enable(
                transport_id,
                reason=reason,
                actor=self._actor(),
                subject_id=owner.kernel.subject_id,
            )
        elif operation == "disable":
            record = owner.transports.disable(
                transport_id,
                reason=reason,
                actor=self._actor(),
                subject_id=owner.kernel.subject_id,
            )
        elif operation == "revoke":
            record = owner.transports.revoke(
                transport_id,
                reason=reason,
                actor=self._actor(),
                subject_id=owner.kernel.subject_id,
            )
        else:
            raise ValueError("unsupported transport operation")
    except (ValueError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_transport"})
        return
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "transport_not_found"})
        return
    self._json(
        HTTPStatus.OK,
        {"transport_id": record.transport_id, "status": record.status},
    )


def delivery_operation(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
    parts = self.path.removeprefix("/api/deliveries/").strip("/").split("/")
    reason = payload.get("reason")
    if len(parts) != 2 or not isinstance(reason, str):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_delivery_operation"})
        return
    delivery_id, operation = parts
    try:
        if operation == "lookup":
            record = owner.run_async_from_handler(
                owner.deliveries.lookup_unknown(
                    delivery_id,
                    subject_id=owner.kernel.subject_id,
                    actor=self._actor(),
                    reason=reason,
                )
            )
        elif operation == "reconcile":
            outcome = payload.get("status")
            provider_message_id = payload.get("provider_message_id")
            evidence = payload.get("evidence", {})
            if outcome not in {"delivered", "failed", "cancelled"}:
                raise ValueError("invalid delivery reconciliation status")
            if provider_message_id is not None and not isinstance(provider_message_id, str):
                raise TypeError("provider message id must be text")
            if not isinstance(evidence, dict):
                raise TypeError("delivery reconciliation evidence must be an object")
            record = owner.deliveries.reconcile_unknown(
                delivery_id,
                cast(Literal["delivered", "failed", "cancelled"], outcome),
                subject_id=owner.kernel.subject_id,
                actor=self._actor(),
                reason=reason,
                provider_message_id=provider_message_id,
                evidence=evidence,
            )
        else:
            raise ValueError("unsupported delivery operation")
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "delivery_not_found"})
        return
    except InvalidTransitionError:
        self._json(
            HTTPStatus.CONFLICT,
            {"error": "delivery_reconciliation_conflict"},
        )
        return
    except (ValueError, TypeError, PermissionError):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_delivery_operation"},
        )
        return
    self._json(HTTPStatus.OK, record.__dict__)


def configure_inbound_binding(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
        transport_id = payload.get("transport_id")
        account = payload.get("external_account_id")
        sender = payload.get("external_sender_id")
        role = payload.get("role", "participant")
        label = payload.get("label", "")
        if (
            not isinstance(transport_id, str)
            or not isinstance(account, str)
            or not isinstance(sender, str)
            or not isinstance(role, str)
            or not isinstance(label, str)
        ):
            raise TypeError("binding fields must be text")
        if role not in {"creator", "participant"}:
            raise ValueError("binding role is invalid")
        binding_id = owner.inbound.bind(
            owner.kernel.subject_id,
            transport_id,
            external_account_id=account,
            external_sender_id=sender,
            role=cast(Literal["creator", "participant"], role),
            label=label,
        )
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_inbound_binding"})
        return
    self._json(HTTPStatus.CREATED, {"binding_id": binding_id, "status": "active"})


def identity_challenge(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
        if self.path == "/api/config/identity-challenges":
            if set(payload) != {"transport_id"} or not isinstance(payload["transport_id"], str):
                raise ValueError("invalid challenge")
            result = owner.inbound.issue_identity_challenge(
                owner.kernel.subject_id, payload["transport_id"]
            )
        else:
            parts = self.path.removeprefix("/api/config/identity-challenges/").split("/")
            if len(parts) != 2 or parts[1] != "confirm" or set(payload) - {"label"}:
                raise ValueError("invalid challenge confirmation")
            label = payload.get("label", "我的 QQ")
            if not isinstance(label, str):
                raise ValueError("invalid challenge label")
            binding = owner.inbound.confirm_identity_challenge(
                owner.kernel.subject_id, parts[0], label=label
            )
            result = {"binding_id": binding, "status": "confirmed"}
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "identity_challenge_not_found"})
        return
    except (ValueError, TypeError):
        self._json(HTTPStatus.CONFLICT, {"error": "identity_challenge_unavailable"})
        return
    self._json(HTTPStatus.OK, result)


def change_inbound_binding(self: RequestHandler, owner: NoyraHTTPServer) -> None:
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
    parts = self.path.removeprefix("/api/config/inbound-bindings/").strip("/").split("/")
    reason = payload.get("reason")
    if len(parts) != 2 or not isinstance(reason, str):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_inbound_binding"})
        return
    binding_id, operation = parts
    if operation == "unlink":
        try:
            owner.inbound.unlink_private_chat(owner.kernel.subject_id, binding_id)
        except NotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "inbound_binding_not_found"})
            return
        self._json(HTTPStatus.OK, {"status": "unlinked"})
        return
    if operation == "route":
        try:
            conversation_id = payload.get("conversation_id")
            share = payload.get("share_web_history")
            push = payload.get("push_web_replies", False)
            if (
                not isinstance(conversation_id, str)
                or type(share) is not bool
                or type(push) is not bool
            ):
                raise ValueError("invalid conversation route")
            owner.inbound.route_private_chat(
                owner.kernel.subject_id,
                binding_id,
                conversation_id,
                share_web_history=share,
                push_web_replies=push,
                expected_revision=payload.get("expected_revision"),
            )
        except (ValueError, TypeError, NotFoundError, sqlite3.IntegrityError):
            self._json(HTTPStatus.CONFLICT, {"error": "private_chat_route_unavailable"})
            return
        self._json(HTTPStatus.OK, {"status": "linked"})
        return
    status = {"enable": "active", "disable": "disabled", "revoke": "revoked"}.get(operation)
    if status is None:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_inbound_binding"})
        return
    try:
        result = owner.inbound.change_binding(
            binding_id,
            owner.kernel.subject_id,
            status=cast(Literal["active", "disabled", "revoked"], status),
            actor=self._actor(),
            reason=reason,
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "inbound_binding_not_found"})
        return
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_inbound_binding"})
        return
    self._json(HTTPStatus.OK, result)
