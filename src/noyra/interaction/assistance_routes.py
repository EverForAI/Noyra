"""Immutable destinations for externally delivered public project questions."""

from __future__ import annotations

from typing import Any

from noyra.core.database import Database
from noyra.core.delivery_state import effective_delivery_status
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.types import content_hash, utc_now

_ROUTE_FIELDS = {
    "version",
    "request_id",
    "interaction_id",
    "delivery_id",
    "transport_id",
    "channel",
    "account_id",
    "recipient",
}


class AssistanceRoutes:
    def __init__(self, database: Database, subject: str):
        self.database = database
        self.subject = subject

    @staticmethod
    def event_id(delivery_id: str) -> str:
        return "evt_help_route_" + content_hash(delivery_id)

    def read(self, connection: Any, delivery_id: str) -> dict[str, Any] | None:
        event = connection.execute(
            "SELECT * FROM events WHERE subject_id=? AND event_id=?",
            (self.subject, self.event_id(delivery_id)),
        ).fetchone()
        if event is None:
            return None
        payload = EventStore(self.database).payload_from_row(event, connection=connection)
        if (
            event["event_type"] != "assistance_delivery_route"
            or event["source"] != "system"
            or event["privacy_level"] != "private"
            or content_hash(payload) != event["payload_hash"]
            or set(payload) != _ROUTE_FIELDS
            or type(payload["version"]) is not int
            or payload["version"] != 1
            or any(
                not isinstance(payload[key], str) or not payload[key].strip()
                for key in _ROUTE_FIELDS - {"version", "account_id"}
            )
            or (
                payload["account_id"] is not None
                and (
                    not isinstance(payload["account_id"], str) or not payload["account_id"].strip()
                )
            )
            or payload["delivery_id"] != delivery_id
        ):
            raise IntegrityError("help delivery route evidence is invalid")
        owner = connection.execute(
            "SELECT i.idempotency_key,i.content,t.channel FROM interaction_deliveries d "
            "JOIN interactions i ON i.interaction_id=d.interaction_id "
            "AND i.subject_id=d.subject_id "
            "JOIN interaction_transports t ON t.transport_id=d.transport_id "
            "AND t.subject_id=d.subject_id JOIN autonomous_project_assistance_requests r "
            "ON r.subject_id=d.subject_id AND r.request_id=? "
            "WHERE d.subject_id=? AND d.delivery_id=? AND d.interaction_id=? "
            "AND d.transport_id=? AND i.channel='web' AND i.kind='help_request' "
            "AND i.direction='outgoing' AND i.content=r.public_summary",
            (
                payload["request_id"],
                self.subject,
                delivery_id,
                payload["interaction_id"],
                payload["transport_id"],
            ),
        ).fetchone()
        if (
            owner is None
            or owner["channel"] != payload["channel"]
            or owner["idempotency_key"] != "project-assistance:" + payload["request_id"]
        ):
            raise IntegrityError("help delivery route ownership mismatch")
        return payload

    def freeze(self, delivery_id: str, recipient: str) -> dict[str, Any] | None:
        """Capture the actual destination before send; never infer old sent routes."""
        from .transport import TransportStore

        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT i.*,d.transport_id,d.status AS delivery_status,t.channel AS target_channel,"
                "t.config_json,t.status AS transport_status FROM interaction_deliveries d "
                "JOIN interactions i ON i.interaction_id=d.interaction_id "
                "AND i.subject_id=d.subject_id "
                "JOIN interaction_transports t ON t.transport_id=d.transport_id "
                "AND t.subject_id=d.subject_id WHERE d.subject_id=? AND d.delivery_id=?",
                (self.subject, delivery_id),
            ).fetchone()
            if (
                row is None
                or row["channel"] != "web"
                or row["kind"] != "help_request"
                or not str(row["idempotency_key"]).startswith("project-assistance:")
            ):
                return None
            existing = self.read(connection, delivery_id)
            if existing is not None:
                return existing
            if row["delivery_status"] != "sending" or row["transport_status"] != "active":
                raise PermissionError("help delivery is unavailable")
            request_id = str(row["idempotency_key"])[len("project-assistance:") :]
            request = connection.execute(
                "SELECT status,public_summary FROM autonomous_project_assistance_requests "
                "WHERE subject_id=? AND request_id=?",
                (self.subject, request_id),
            ).fetchone()
            if (
                request is None
                or request["status"] != "open"
                or request["public_summary"] != row["content"]
            ):
                raise PermissionError("help request is unavailable")
            account = TransportStore._config(row).get("account_id")
            payload = {
                "version": 1,
                "request_id": request_id,
                "interaction_id": str(row["interaction_id"]),
                "delivery_id": delivery_id,
                "transport_id": str(row["transport_id"]),
                "channel": str(row["target_channel"]),
                "account_id": account if isinstance(account, str) and account.strip() else None,
                "recipient": recipient,
            }
            EventStore(self.database)._append_connection(
                connection,
                self.subject,
                "assistance_delivery_route",
                "system",
                payload,
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=utc_now(),
                event_id=self.event_id(delivery_id),
            )
            return payload

    def reply_parent(
        self,
        connection: Any,
        transport_id: str,
        channel: str,
        account: str,
        sender: str,
        reference: str,
    ) -> tuple[str, str, str] | None:
        # Candidate lookup is bounded and reference-based, not an event-history scan.
        candidates = connection.execute(
            "SELECT d.delivery_id,d.interaction_id,d.subject_id,d.status "
            "FROM interaction_deliveries d "
            "JOIN interactions i ON i.interaction_id=d.interaction_id "
            "AND i.subject_id=d.subject_id "
            "WHERE d.subject_id=? AND d.transport_id=? AND d.provider_message_id=? "
            "AND d.status IN ('delivered','unknown') AND i.channel='web' "
            "AND i.direction='outgoing' AND i.kind='help_request' LIMIT 2",
            (self.subject, transport_id, reference),
        ).fetchall()
        if len(candidates) != 1:
            return None
        if effective_delivery_status(connection, candidates[0]) not in {"delivered", "unknown"}:
            return None
        route = self.read(connection, str(candidates[0]["delivery_id"]))
        if (
            route is None
            or route["channel"] != channel
            or route["account_id"] != account
            or route["recipient"] != sender
        ):
            return None
        return (
            str(route["interaction_id"]),
            str(route["request_id"]),
            content_hash(["help-delivery:v1", route["delivery_id"], channel, account, sender]),
        )

    def inherited_request(self, connection: Any, parent_id: str) -> str | None:
        row = connection.execute(
            "SELECT i.related_interaction_id,p.entity_id FROM interactions i "
            "JOIN participant_message_links p ON p.interaction_id=i.interaction_id "
            "AND p.subject_id=i.subject_id WHERE i.subject_id=? AND i.interaction_id=? "
            "AND i.direction='outgoing' AND p.visibility='private'",
            (self.subject, parent_id),
        ).fetchone()
        if row is None or row["related_interaction_id"] is None:
            return None
        event = connection.execute(
            "SELECT * FROM events WHERE subject_id=? AND event_id=? "
            "AND event_type='assistance_reply'",
            (self.subject, "evt_help_reply_" + content_hash(row["related_interaction_id"])),
        ).fetchone()
        if event is None:
            return None
        payload = EventStore(self.database).payload_from_row(event, connection=connection)
        if (
            content_hash(payload) != event["payload_hash"]
            or payload.get("interaction_id") != row["related_interaction_id"]
            or payload.get("entity_id") != row["entity_id"]
            or not isinstance(payload.get("request_id"), str)
        ):
            raise IntegrityError("help follow-up linkage is invalid")
        return str(payload["request_id"])
