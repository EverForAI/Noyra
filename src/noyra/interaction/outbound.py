"""Platform-specific outbound message envelopes.

The dispatcher owns retries and delivery state; adapters only build the
provider request URL and payload.  Keeping these contracts separate prevents
one provider's payload quirks from silently leaking into another channel.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True)
class OutboundRequest:
    endpoint: str
    payload: dict[str, Any]
    headers: dict[str, str] | None = None


class OutboundAdapter(Protocol):
    def build(
        self,
        endpoint: str,
        credentials: dict[str, Any],
        interaction: Any,
        idempotency_key: str,
    ) -> OutboundRequest: ...


class TelegramOutboundAdapter:
    def build(
        self, endpoint: str, credentials: dict[str, Any], interaction: Any, idempotency_key: str
    ) -> OutboundRequest:
        token = str(credentials.get("bot_token", ""))
        if not token:
            raise ValueError("telegram bot_token is required")
        payload = {"chat_id": interaction["counterparty"], "text": interaction["content"]}
        if interaction.get("thread_id") is not None:
            payload["message_thread_id"] = interaction["thread_id"]
        return OutboundRequest(endpoint.rstrip("/") + f"/bot{token}/sendMessage", payload)


class FeishuOutboundAdapter:
    def build(
        self, endpoint: str, credentials: dict[str, Any], interaction: Any, idempotency_key: str
    ) -> OutboundRequest:
        # Feishu custom bots use the webhook URL and do not need an auth
        # header.  App bots use the native IM API and require a tenant access
        # token plus an explicit receive_id_type.
        parsed = urlsplit(endpoint)
        configured_mode = str(credentials.get("delivery_mode", "")).casefold()
        is_app_api = configured_mode == "app" or bool(
            credentials.get("tenant_access_token")
            or credentials.get("app_id")
            or credentials.get("app_secret")
        )
        if is_app_api and not parsed.path.rstrip("/").endswith("/open-apis/im/v1/messages"):
            endpoint = urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    "/open-apis/im/v1/messages",
                    parsed.query,
                    parsed.fragment,
                )
            )
            parsed = urlsplit(endpoint)
        if is_app_api:
            token = str(credentials.get("tenant_access_token", ""))
            if not token:
                raise ValueError("feishu tenant_access_token is required for app API")
            receive_id_type = str(credentials.get("receive_id_type", "open_id"))
            if receive_id_type not in {"open_id", "user_id", "union_id", "email", "chat_id"}:
                raise ValueError("feishu receive_id_type is invalid")
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            query["receive_id_type"] = receive_id_type
            endpoint = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
            )
            return OutboundRequest(
                endpoint,
                {
                    "receive_id": str(interaction["counterparty"]),
                    "msg_type": "text",
                    "content": _json_text({"text": interaction["content"]}),
                    "uuid": idempotency_key,
                },
                {"Authorization": f"Bearer {token}"},
            )
        payload: dict[str, Any] = {
            "msg_type": "text",
            # Custom bot webhooks use an object here.  The app IM API above
            # deliberately uses a JSON string because that is a different
            # Feishu endpoint contract.
            "content": {"text": interaction["content"]},
        }
        # Custom bot signing is optional in Feishu, but when a verification
        # key is configured the timestamp/sign fields are mandatory.
        signing_secret = str(credentials.get("signing_secret", ""))
        if signing_secret:
            timestamp = str(int(time.time()))
            string_to_sign = f"{timestamp}\n{signing_secret}".encode()
            # Feishu custom-bot signing uses ``timestamp + newline + secret``
            # as the HMAC-SHA256 key and an empty message.  This unusual
            # provider contract is intentionally not interchangeable with a
            # plain SHA-256 digest or with the inbound Encrypt Key signature.
            digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(digest).decode("ascii")
        return OutboundRequest(endpoint, payload)


class QQOutboundAdapter:
    def build(
        self, endpoint: str, credentials: dict[str, Any], interaction: Any, idempotency_key: str
    ) -> OutboundRequest:
        token = str(credentials.get("access_token", ""))
        if not token:
            raise ValueError("qq access_token is required")
        target_type = str(credentials.get("target_type", "user"))
        target = str(interaction.get("counterparty", ""))
        if not target:
            raise ValueError("qq recipient openid is required")
        # QQ places the recipient identifier in the URL path.  Quote it as a
        # single segment so an inbound-controlled identifier cannot alter the
        # provider route or escape the configured official host.
        target_segment = quote(target, safe="")
        if target_type == "group":
            path = f"/v2/groups/{target_segment}/messages"
        elif target_type == "channel":
            path = f"/channels/{target_segment}/messages"
        elif target_type == "dm":
            path = f"/dms/{target_segment}/messages"
        elif target_type == "user":
            path = f"/v2/users/{target_segment}/messages"
        else:
            raise ValueError("qq target_type is invalid")
        parsed = urlsplit(endpoint.rstrip("/"))
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("qq endpoint is invalid")
        endpoint = urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))
        payload = {"content": interaction["content"], "msg_type": 0}
        # QQ accepts msg_id to make a reply to a provider message.  The
        # persisted idempotency key is only a fallback, never a fake target.
        if interaction.get("thread_id"):
            payload["msg_id"] = str(interaction["thread_id"])
            if target_type in {"user", "group"}:
                # QQ's C2C/group reply contract requires a 16-bit msg_seq.
                # Derive it from the durable delivery idempotency key so a
                # retry keeps the same provider-side deduplication tuple.
                payload["msg_seq"] = int.from_bytes(
                    hashlib.sha256(idempotency_key.encode("utf-8")).digest()[:2],
                    "big",
                )
        return OutboundRequest(endpoint, payload, {"Authorization": f"QQBot {token}"})


class WeChatOutboundAdapter:
    def build(
        self, endpoint: str, credentials: dict[str, Any], interaction: Any, idempotency_key: str
    ) -> OutboundRequest:
        token = str(credentials.get("access_token", ""))
        if not token:
            raise ValueError("wechat access_token is required")
        recipient = str(interaction.get("counterparty", ""))
        if not recipient:
            raise ValueError("wechat recipient openid is required")
        parsed = urlsplit(endpoint)
        if not parsed.path.rstrip("/").endswith("/cgi-bin/message/custom/send"):
            endpoint = urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    "/cgi-bin/message/custom/send",
                    parsed.query,
                    parsed.fragment,
                )
            )
            parsed = urlsplit(endpoint)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["access_token"] = token
        endpoint = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
        )
        return OutboundRequest(
            endpoint,
            {
                "touser": recipient,
                "msgtype": "text",
                "text": {"content": interaction["content"]},
            },
        )


class WebhookOutboundAdapter:
    def build(
        self, endpoint: str, credentials: dict[str, Any], interaction: Any, idempotency_key: str
    ) -> OutboundRequest:
        return OutboundRequest(
            endpoint,
            {
                "event_type": "noyra.interaction",
                "message_id": idempotency_key,
                "channel": interaction["channel"],
                "conversation_id": interaction["counterparty"],
                "text": interaction["content"],
            },
        )


OUTBOUND_ADAPTERS: dict[str, OutboundAdapter] = {
    "telegram": TelegramOutboundAdapter(),
    "feishu": FeishuOutboundAdapter(),
    "qq": QQOutboundAdapter(),
    "wechat": WeChatOutboundAdapter(),
    "webhook": WebhookOutboundAdapter(),
}


def _json_text(value: dict[str, Any]) -> str:
    # Providers expect the nested content field to be a JSON string.
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
